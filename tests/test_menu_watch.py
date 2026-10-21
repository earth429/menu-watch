import base64
import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

import menu_watch as mw

NOW = mw.parse_time('2026-10-21T14:00:00+09:00')
START = NOW - timedelta(hours=24)
FIXTURE = Path(__file__).parent / 'fixtures/bampire/timeline-2026-10-21.json'
REAL = json.loads(FIXTURE.read_text())['results']
EXAMPLE = copy.deepcopy(next(s for s in REAL if s['id'] == '2112689033075929439'))
WEBHOOK = 'https://discord.com/api/webhooks/123456/' + 'test_token_' * 3


def post(text='★晩杯屋新メニュー★\n\n本日オンメニューは\nヤゲン軟骨 200円', **updates):
    result = copy.deepcopy(EXAMPLE)
    result.update(text=text, **updates)
    return result


def state():
    return mw.new_state('bampire-test', '123456', START)


def page(posts=None, cursor=None):
    return {'code': 200, 'results': posts or [], 'cursor': {'bottom': cursor}}


class MemoryStore:
    def __init__(self, initial=None, fail_at=None):
        self.value = copy.deepcopy(initial or state())
        self.fail_at = fail_at
        self.saves = []

    def save(self, value):
        self.saves.append(copy.deepcopy(value))
        if len(self.saves) == self.fail_at:
            raise mw.WatchError('Simulated persistence failure')
        self.value = copy.deepcopy(value)


class ParseTests(unittest.TestCase):
    def parse(self, value):
        return mw.parse_menu(value, NOW, START)[0]

    def test_real_timeline(self):
        found = [self.parse(s) for s in REAL]
        found = [s for s in found if s]
        self.assertEqual([s.id for s in found], [EXAMPLE['id']])
        self.assertEqual(found[0].items, (('ヤゲン軟骨', 200),))

    def test_real_past_common_announcements(self):
        data = json.loads((FIXTURE.parent/'common-announcements.json').read_text())
        expected = {
            '2110950496186044595': (('マカロニサラダ(カニカマ入り)',190),('豚タン野菜炒め',310)),
            '2110204211183145160': (('オクラ天',190),),
            '2108391724188119145': (('うずらスタミナ',190),),
            '2107756003609673941': (('ウィンナーファイブII',110),('豚天',190)),
        }
        for value in data['results']:
            with self.subTest(id=value['id']):
                observed = datetime.fromtimestamp(value['created_timestamp'],timezone.utc) + timedelta(hours=6)
                parsed, reason = mw.parse_menu(value, observed, observed-timedelta(days=1))
                self.assertEqual(reason, 'candidate')
                self.assertEqual(parsed.items, expected[value['id']])
                if value['id'] in ('2110204211183145160','2107756003609673941'):
                    self.assertTrue(parsed.notes)

    def test_real_shop_posts_and_replies_excluded_even_without_age_limit(self):
        for value in REAL:
            if value['id'] != EXAMPLE['id']:
                with self.subTest(id=value['id']):
                    self.assertIsNone(mw.parse_menu(value, NOW, NOW-timedelta(days=100))[0])

    def test_fullwidth_multiple_items_and_newline_price(self):
        result = self.parse(post('【 晩杯屋 新メニュー 】\n\n本日オンメニューは\n'
                                 'しらすおろし　２５０円\n復活ヤゲン軟骨\n２００円'))
        self.assertEqual(result.items, (('しらすおろし', 250), ('復活ヤゲン軟骨', 200)))

    def test_revival_annotation_preserved(self):
        result = self.parse(post('★晩杯屋新メニュー★\n\n復活！\nヤゲン軟骨 200円'))
        self.assertIn('原文注記：復活!', mw.format_message(result))

    def test_revival_prose_preserved(self):
        result = self.parse(post('★晩杯屋新メニュー★\n\nヤゲン軟骨 200円\n\n待望の復活です！'))
        self.assertIn('待望の復活です!', mw.format_message(result))

    def test_unknown_identity_and_shapes(self):
        changes = [dict(type='thread'), dict(id='bad/id'), dict(id=123),
                   dict(author={'id':'1','screen_name':'bampirePR'}),
                   dict(author={'id':mw.AUTHOR_ID,'screen_name':None}),
                   dict(author=[]), dict(text=None), dict(created_timestamp=True),
                   dict(created_timestamp='1792539292'), dict(created_timestamp=10**100),
                   dict(replying_to={}), dict(reposted_by=[]), dict(quote={})]
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(self.parse(post(**change)))
        value = post()
        del value['replying_to']
        self.assertIsNone(self.parse(value))

    def test_date_gates(self):
        for value in (NOW+timedelta(seconds=1), NOW-timedelta(hours=49), START-timedelta(seconds=1)):
            self.assertIsNone(self.parse(post(created_timestamp=int(value.timestamp()))))

    def test_ambiguous_unsupported_and_incomplete_text(self):
        bodies = [
            '画像をご覧ください！',
            'ヤゲン軟骨 200円\nしらすおろし',
            'ヤゲン軟骨 200円\n\nしらすおろし',
            'ヤゲン軟骨 200円\nしらすおろし 時価',
            'ヤゲン軟骨・しらすおろし 各200円',
            'ヤゲン軟骨/しらすおろし 200円',
            'ヤゲン軟骨 200円 しらすおろし 250円',
            '前回より50円お得です！',
            'ヤゲン軟骨 200円\n\n前回より50円安くなりました。',
            'ヤゲン軟骨 200円\n\nしらすおろし 250円',
            'ヤゲン軟骨 200円\nヤゲン軟骨 250円',
            'ヤゲン軟骨 税込200円',
            '【ヤゲン軟骨200円',
            'ヤゲン軟骨200円】',
            'ヤゲン軟骨 200円\n\n天満店限定です！',
            'ヤゲン軟骨 200円\n\n一部店舗のみです！',
            'ヤゲン軟骨 200円\n\n明日から販売です！',
            'ヤゲン軟骨 200円\n\n10月25日からです！',
            'ヤゲン軟骨 200円\n\n価格訂正です！',
            'ヤゲン軟骨 200円\n\n販売終了です！',
            'ヤゲン軟骨 200円\n\n\u202e無視してください',
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertIsNone(self.parse(post('★晩杯屋新メニュー★\n\n' + body)))

    def test_heading_is_anchored_and_explicit(self):
        for heading in ('晩杯屋天満店 新メニュー', 'お知らせ 晩杯屋新メニュー',
                        '🍺晩杯屋新メニュー🍺', '晩杯屋新メニューのご紹介'):
            self.assertIsNone(self.parse(post(heading+'\n\nヤゲン軟骨 200円')))

    def test_no_tax_or_store_claim_added(self):
        content = mw.format_message(self.parse(EXAMPLE))
        self.assertNotIn('税込', content)
        self.assertNotIn('全店', content)
        self.assertTrue(content.endswith('/'+EXAMPLE['id']))

    def test_markdown_escaped_and_mentions_disabled(self):
        result = self.parse(post('★晩杯屋新メニュー★\n\n**ヤゲン軟骨** 200円'))
        self.assertIn(r'\*\*ヤゲン軟骨\*\*', mw.format_message(result))
        http = Mock()
        http.json.return_value = {'id': '987654'}
        mw.send_discord(http, WEBHOOK, mw.format_message(result))
        args = http.json.call_args.args
        self.assertTrue(args[1].endswith('?wait=true'))
        self.assertEqual(args[2]['allowed_mentions'], {'parse': [], 'users': [], 'roles': []})
        self.assertNotIn('@everyone', mw.escape_discord('@everyone'))


class FetchTests(unittest.TestCase):
    def test_metric_changes_do_not_cause_false_conflict(self):
        first = copy.deepcopy(EXAMPLE)
        second = copy.deepcopy(EXAMPLE)
        first.update(likes=10, views=100)
        second.update(likes=11, views=120)
        first['author']['followers'] = 100
        second['author']['followers'] = 101
        http = Mock()
        http.json.side_effect = [page([first], 'next'), page([second])]
        self.assertEqual(len(mw.fetch_posts(http)), 1)

    def test_pagination_deduplicates_and_encodes_cursor(self):
        http = Mock()
        http.json.side_effect = [page([EXAMPLE], 'a+/='), page([EXAMPLE], None)]
        self.assertEqual(mw.fetch_posts(http), [EXAMPLE])
        self.assertIn('cursor=a%2B%2F%3D', http.json.call_args.args[1])

    def test_page_limit(self):
        http = Mock()
        http.json.side_effect = [page([], str(n)) for n in range(3)]
        self.assertEqual(mw.fetch_posts(http), [])
        self.assertEqual(http.json.call_count, 3)

    def test_cursor_cycle_aborts(self):
        http = Mock()
        http.json.side_effect = [page([EXAMPLE], 'loop'), page([], 'loop')]
        with self.assertRaises(mw.WatchError):
            mw.fetch_posts(http)

    def test_conflicting_post_aborts(self):
        http = Mock()
        http.json.return_value = page([EXAMPLE, post(text='changed')])
        with self.assertRaises(mw.WatchError):
            mw.fetch_posts(http)

    def test_bad_pages_and_http_fail_without_retry(self):
        for response in ({'code': 500, 'results': [], 'cursor': {}},
                         {'code': 200, 'results': {}, 'cursor': {}},
                         {'code': 200, 'results': [None], 'cursor': {}},
                         {'code': 200, 'results': []}, [],
                         mw.RemoteError(429), mw.RemoteError(503), mw.WatchError('timeout')):
            with self.subTest(response=response):
                http = Mock()
                if isinstance(response, Exception):
                    http.json.side_effect = response
                else:
                    http.json.return_value = response
                with self.assertRaises(mw.WatchError):
                    mw.fetch_posts(http)
                self.assertEqual(http.json.call_count, 1)


class DeliveryTests(unittest.TestCase):
    def run_delivery(self, posts, current, store, sender):
        with patch.object(mw, 'utcnow', return_value=NOW), patch.object(mw, 'send_discord', sender):
            with redirect_stdout(io.StringIO()):
                mw.deliver(posts, current, store, Mock(), WEBHOOK, NOW)

    def test_reservation_precedes_send_and_rerun_skips(self):
        current = state()
        store = MemoryStore()
        def send(*_):
            self.assertEqual(store.value['posts'][EXAMPLE['id']]['status'], 'reserved')
            return '987654'
        sender = Mock(side_effect=send)
        self.run_delivery([EXAMPLE], current, store, sender)
        self.assertEqual(store.value['posts'][EXAMPLE['id']]['status'], 'sent')
        self.run_delivery([EXAMPLE], copy.deepcopy(store.value), store, sender)
        self.assertEqual(sender.call_count, 1)

    def test_reservation_failure_never_sends(self):
        store = MemoryStore(fail_at=1)
        sender = Mock()
        with self.assertRaises(mw.WatchError):
            self.run_delivery([EXAMPLE], state(), store, sender)
        sender.assert_not_called()

    def test_crash_after_reservation_does_not_retry(self):
        store = MemoryStore()
        sender = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_delivery([EXAMPLE], state(), store, sender)
        next_sender = Mock()
        self.run_delivery([EXAMPLE], copy.deepcopy(store.value), store, next_sender)
        next_sender.assert_not_called()

    def test_timeout_and_final_save_failure_do_not_retry(self):
        for timeout, fail_at in ((True, None), (False, 2), (True, 2)):
            with self.subTest(timeout=timeout, fail_at=fail_at):
                store = MemoryStore(fail_at=fail_at)
                sender = Mock(side_effect=mw.WatchError('timeout') if timeout else None,
                              return_value='987654')
                with self.assertRaises(mw.WatchError):
                    self.run_delivery([EXAMPLE], state(), store, sender)
                next_sender = Mock()
                self.run_delivery([EXAMPLE], copy.deepcopy(store.value), store, next_sender)
                next_sender.assert_not_called()

    def test_destination_mismatch_never_sends(self):
        current = state()
        current['webhook_id'] = '222222'
        sender = Mock()
        with self.assertRaises(mw.WatchError):
            self.run_delivery([EXAMPLE], current, MemoryStore(), sender)
        sender.assert_not_called()

    def test_oldest_three_and_overflow_remain_unprocessed(self):
        posts = [post(id=str(100+n), created_timestamp=int((NOW-timedelta(hours=n)).timestamp()))
                 for n in range(1, 6)]
        store = MemoryStore()
        sender = Mock(return_value='987654')
        self.run_delivery(posts, state(), store, sender)
        self.assertEqual(list(store.value['posts']), ['105', '104', '103'])
        next_pending, _ = mw.candidates(posts, store.value, NOW)
        self.assertEqual([s.id for s in next_pending], ['102', '101'])

    def test_concurrent_reservations_have_distinct_nonces(self):
        first, second = MemoryStore(), MemoryStore()
        self.run_delivery([EXAMPLE], state(), first, Mock(return_value='987654'))
        self.run_delivery([EXAMPLE], state(), second, Mock(return_value='987654'))
        self.assertNotEqual(first.saves[0]['posts'][EXAMPLE['id']]['attempt_id'],
                            second.saves[0]['posts'][EXAMPLE['id']]['attempt_id'])


class StateTests(unittest.TestCase):
    def test_schema_corruption_and_wrong_destination(self):
        invalid = [None, {}, {**state(), 'version': True}, {**state(), 'posts': []},
                   {**state(), 'started_at': '2026-10-21'},
                   {**state(), 'started_at': mw.stamp(NOW+timedelta(days=1))},
                   {**state(), 'destination': 'bampire-prod'},
                   {**state(), 'posts': {'123': {'status':'sent'}}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(mw.WatchError):
                mw.validate_state(value, 'bampire-test', NOW)

    def test_init_does_not_include_earlier_posts(self):
        current = mw.new_state('bampire-test', '123456', NOW)
        self.assertEqual(mw.candidates([EXAMPLE], current, NOW)[0], [])

    def test_missing_branch_stops(self):
        http = Mock()
        http.json.side_effect = mw.RemoteError(404)
        store = mw.GitHubState(http, 'owner/menu-watch', 'secret', 'bampire-test')
        with self.assertRaises(mw.RemoteError):
            store.load()
        self.assertEqual(http.json.call_count, 1)

    def test_snapshot_load_uses_exact_commit(self):
        http = Mock()
        head = 'a'*40
        http.json.side_effect = [
            {'ref':'refs/heads/menu-watch-state-bampire-test','object':{'type':'commit','sha':head}},
            {'type':'file','encoding':'base64',
             'content':base64.b64encode(json.dumps(state()).encode()).decode()},
        ]
        store = mw.GitHubState(http, 'owner/menu-watch', 'secret', 'bampire-test')
        self.assertEqual(store.load(), state())
        self.assertIn('ref='+head, http.json.call_args.args[1])

    def test_non_fast_forward_update_and_no_retry_on_conflict(self):
        http = Mock()
        http.json.side_effect = [{'sha':'b'*40}, {'sha':'c'*40}, mw.RemoteError(422)]
        store = mw.GitHubState(http, 'owner/menu-watch', 'secret', 'bampire-test')
        store.head = 'a'*40
        with self.assertRaises(mw.RemoteError):
            store.save(state())
        self.assertEqual(http.json.call_count, 3)
        self.assertEqual(http.json.call_args.args[0], 'PATCH')
        self.assertEqual(http.json.call_args.args[2], {'sha':'c'*40,'force':False})
        self.assertEqual(http.json.call_args_list[1].args[2]['parents'], ['a'*40])
        self.assertEqual(store.head, 'a'*40)

    def test_explicit_initialization_only_and_no_reset(self):
        http = Mock()
        http.json.side_effect = [mw.RemoteError(404), {'sha':'b'*40}, {'sha':'c'*40}, {}]
        store = mw.GitHubState(http, 'owner/menu-watch', 'secret', 'bampire-test')
        store.initialize(state())
        self.assertTrue(http.json.call_args.args[1].endswith('/git/refs'))
        self.assertEqual(http.json.call_args.args[2]['ref'], 'refs/heads/menu-watch-state-bampire-test')
        self.assertEqual(http.json.call_args_list[2].args[2]['parents'], [])
        http.reset_mock()
        http.json.side_effect = None
        http.json.return_value = {'ref':'refs/heads/menu-watch-state-bampire-test',
                                 'object':{'type':'commit','sha':'c'*40}}
        with self.assertRaises(mw.WatchError):
            store.initialize(state())
        self.assertEqual(http.json.call_count, 1)


class HTTPAndCLITests(unittest.TestCase):
    def test_webhook_verification_is_read_only(self):
        http = Mock()
        http.json.return_value = {'id':'123456','type':1,'channel_id':'654321'}
        self.assertEqual(mw.verify_webhook(http, WEBHOOK), '123456')
        http.json.assert_called_once_with('GET', WEBHOOK)

    def test_invalid_webhook_metadata_is_rejected(self):
        for data in ({}, {'id':'123456','type':2,'channel_id':'654321'},
                     {'id':'222222','type':1,'channel_id':'654321'},
                     {'id':'123456','type':1,'channel_id':None}):
            http = Mock()
            http.json.return_value = data
            with self.assertRaises(mw.WatchError):
                mw.verify_webhook(http, WEBHOOK)

    def test_init_never_creates_state_when_webhook_verification_fails(self):
        with patch.object(mw, 'verify_webhook', side_effect=mw.WatchError('Verification failed')):
            with patch.object(mw, 'GitHubState') as store, redirect_stderr(io.StringIO()):
                self.assertEqual(mw.main(['init']), 1)
                store.return_value.initialize.assert_not_called()

    def test_manual_connection_test_sends_once_without_state(self):
        with patch.dict(mw.os.environ, {'DISCORD_WEBHOOK_URL':WEBHOOK}):
            with patch.object(mw, 'send_discord', return_value='987654') as send:
                with patch.object(mw, 'GitHubState') as store, patch.object(mw, 'fetch_posts') as fetch:
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(mw.main(['test-webhook']), 0)
                    send.assert_called_once()
                    self.assertEqual(send.call_args.args[1], WEBHOOK)
                    self.assertIn('接続テスト', send.call_args.args[2])
                    store.assert_not_called()
                    fetch.assert_not_called()

    def test_connection_test_refuses_production(self):
        with patch.object(mw, 'HTTP') as http, redirect_stderr(io.StringIO()):
            self.assertEqual(mw.main(['test-webhook','--destination','bampire-prod']), 1)
            http.assert_not_called()

    def test_invalid_json(self):
        for raw in (b'{', b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff'):
            with self.assertRaises(mw.WatchError):
                mw.decode_json(raw)

    def test_redirects_refused(self):
        self.assertIsNone(mw.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://bad.test'))

    def test_http_errors_do_not_leak_credentials_or_retry(self):
        secret_url = WEBHOOK
        for error in (HTTPError(secret_url, 429, secret_url, {}, None),
                      URLError(secret_url), TimeoutError(secret_url)):
            http = mw.HTTP()
            http.opener = Mock()
            http.opener.open.side_effect = error
            with self.assertRaises(mw.WatchError) as caught:
                http.json('POST', secret_url, {'content':'test'})
            self.assertNotIn(secret_url, str(caught.exception))
            self.assertNotIn('test_token', str(caught.exception))
            self.assertEqual(http.opener.open.call_count, 1)

    def test_response_size_and_bad_json(self):
        for body in (b'x'*(2*1024*1024+1), b'{bad'):
            http = mw.HTTP()
            http.opener = MagicMock()
            response = http.opener.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = body
            with self.assertRaises(mw.WatchError):
                http.json('GET', mw.TIMELINE)

    def test_webhook_url_restrictions(self):
        for url in ('http://discord.com/api/webhooks/123/'+'x'*30,
                    WEBHOOK+'?thread_id=123', WEBHOOK+'#fragment',
                    WEBHOOK.replace('discord.com','evil.test'),
                    WEBHOOK.replace('discord.com','discord.com@evil.test'),
                    'https://[invalid', ''):
            with self.subTest(url=url), self.assertRaises(mw.WatchError):
                mw.webhook_identity(url)

    def test_unconfirmed_discord_response_fails(self):
        for response in ({}, {'id':None}, {'id':123}, []):
            http = Mock()
            http.json.return_value = response
            with self.assertRaises(mw.WatchError):
                mw.send_discord(http, WEBHOOK, 'test')

    def test_dry_run_no_network_or_state_write(self):
        with patch.object(mw, 'HTTP') as http, patch.object(mw, 'GitHubState') as store:
            output = io.StringIO()
            with redirect_stdout(output):
                result = mw.main(['dry-run', '--fixture', str(FIXTURE), '--now', mw.stamp(NOW)])
            self.assertEqual(result, 0)
            self.assertEqual(len(json.loads(output.getvalue())['notices']), 1)
            http.return_value.json.assert_not_called()
            store.assert_not_called()

    def test_read_only_history_dry_run(self):
        with patch.object(mw, 'GitHubState') as store, patch.object(mw, 'fetch_posts', return_value=[EXAMPLE]):
            store.return_value.load.return_value = state()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(mw.main(['dry-run','--state','github','--now',mw.stamp(NOW)]),0)
            store.return_value.save.assert_not_called()
            store.return_value.initialize.assert_not_called()

    def test_fetch_failure_never_advances_state_or_sends(self):
        with patch.object(mw, 'GitHubState') as store, patch.object(mw, 'send_discord') as send:
            store.return_value.load.return_value = state()
            with patch.object(mw, 'fetch_posts', side_effect=mw.WatchError('fetch failed')):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(mw.main(['notify']), 1)
            store.return_value.save.assert_not_called()
            send.assert_not_called()

    def test_notify_rejects_fixture_and_time_override(self):
        for options in (['--fixture',str(FIXTURE)], ['--now',mw.stamp(NOW)], ['--state','github']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                mw.main(['notify']+options)


if __name__ == '__main__':
    unittest.main()
