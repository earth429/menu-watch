import copy
import io
import json
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import menu_watch as mw

NOW = mw.parse_time('2026-10-21T14:00:00+09:00')
DATA = json.loads((Path(__file__).parent/'fixtures/bampire/timeline-2026-10-21.json').read_text())
POST = next(post for post in DATA['results'] if post['id']=='2112689033075929439')
WEBHOOK = 'https://discord.com/api/webhooks/123456/'+'test_token_'*3
MESSAGE_ID = '987654'


def parsed_post(value=None):
    result, reason = mw.parse_menu(value or POST,NOW,NOW-timedelta(days=1))
    assert result is not None, reason
    return result


def sent_state():
    state = mw.new_state('bampire-test','123456',NOW-timedelta(days=1))
    state['posts'][POST['id']] = {
        'status':'sent','attempt_id':'a'*32,'message_id':MESSAGE_ID,
        'reserved_at':mw.stamp(NOW-timedelta(minutes=2)),
        'finished_at':mw.stamp(NOW-timedelta(minutes=1)),
    }
    return state


class PreviewTests(unittest.TestCase):
    def test_card_has_canonical_source_text_timestamp_and_first_photo(self):
        embed = mw.build_preview(parsed_post())
        self.assertEqual(embed['url'],'https://x.com/bampirePR/status/'+POST['id'])
        self.assertIn('ヤゲン軟骨',embed['description'])
        self.assertEqual(embed['image']['url'],POST['media']['photos'][0]['url'])
        self.assertEqual(mw.parse_time(embed['timestamp']),parsed_post().timestamp)

    def test_nonphoto_or_untrusted_images_are_ignored(self):
        for value in ('http://pbs.twimg.com/media/example.jpg',
                      'https://pbs.twimg.com.evil.test/media/example.jpg',
                      'https://pbs.twimg.com@evil.test/media/example.jpg',
                      'https://pbs.twimg.com/profile_images/example.jpg',
                      'https://pbs.twimg.com/media/example.jpg#fragment',
                      'https://pbs.twimg.com/media/example.jpg\n',None,{},'https://[invalid'):
            with self.subTest(value=value):
                self.assertIsNone(mw.preview_image_url(value))
        post = copy.deepcopy(POST)
        post['media'] = {'photos':[{'type':'video','url':'https://pbs.twimg.com/media/example.jpg'}]}
        self.assertNotIn('image',mw.build_preview(parsed_post(post)))
        post['media'] = {'photos':None}
        self.assertNotIn('image',mw.build_preview(parsed_post(post)))

    def test_card_without_photo_still_shows_text(self):
        post = copy.deepcopy(POST)
        post.pop('media')
        embed = mw.build_preview(parsed_post(post))
        self.assertIn('description',embed)
        self.assertNotIn('image',embed)

    def test_original_style_avoids_duplicate_text_and_preview_can_be_disabled(self):
        original = replace(mw.DEFAULT_SETTINGS,message_style='original')
        self.assertNotIn('description',mw.build_preview(parsed_post(),original))
        disabled = replace(mw.DEFAULT_SETTINGS,link_preview=False)
        self.assertIsNone(mw.build_preview(parsed_post(),disabled))

    def test_description_respects_utf16_limit_and_escapes_mentions(self):
        post = replace(parsed_post(),original_text='@everyone '+'🐙'*3000)
        description = mw.build_preview(post)['description']
        self.assertLessEqual(len(description.encode('utf-16-le'))//2,3000)
        self.assertNotIn('@everyone',description)
        self.assertTrue(description.endswith('…'))

    def test_send_allows_card_without_enabling_mentions(self):
        http = Mock()
        http.json.return_value = {'id':MESSAGE_ID}
        post = parsed_post()
        embed = mw.build_preview(post)
        mw.send_discord(http,WEBHOOK,mw.format_message(post),embed)
        payload = http.json.call_args.args[2]
        self.assertEqual(payload['flags'],0)
        self.assertEqual(payload['embeds'],[embed])
        self.assertEqual(payload['allowed_mentions'],{'parse':[],'users':[],'roles':[]})
        mw.send_discord(http,WEBHOOK,'test',embed,False)
        payload = http.json.call_args.args[2]
        self.assertEqual(payload['flags'],4)
        self.assertNotIn('embeds',payload)


class RefreshTests(unittest.TestCase):
    def current_message(self):
        return {'id':MESSAGE_ID,'webhook_id':'123456','content':mw.format_message(parsed_post()),
                'flags':4,'embeds':[]}

    def test_edits_same_message_with_card_without_changing_history_or_content(self):
        state = sent_state()
        before = copy.deepcopy(state)
        current = self.current_message()
        embed = mw.build_preview(parsed_post())
        updated = {**current,'flags':0,'embeds':[embed]}
        http = Mock()
        http.json.side_effect = [current,updated]
        result = mw.refresh_preview(POST,state,http,WEBHOOK,NOW)
        self.assertEqual(result['status'],'preview-updated')
        self.assertTrue(result['has_image'])
        self.assertEqual(state,before)
        self.assertEqual([call.args[0] for call in http.json.call_args_list],['GET','PATCH'])
        self.assertTrue(http.json.call_args.args[1].endswith('/messages/'+MESSAGE_ID))
        self.assertEqual(http.json.call_args.args[2]['content'],current['content'])
        self.assertEqual(http.json.call_args.args[2]['allowed_mentions']['parse'],[])

    def test_message_mismatch_never_edits(self):
        for change in ({'id':'999'},{'webhook_id':'999'},
                       {'content':'unrelated message'},{'content':None}):
            http = Mock()
            http.json.return_value = {**self.current_message(),**change}
            with self.subTest(change=change), self.assertRaises(mw.WatchError):
                mw.refresh_preview(POST,sent_state(),http,WEBHOOK,NOW)
            self.assertEqual(http.json.call_count,1)

    def test_missing_or_unknown_delivery_never_edits(self):
        for status in (None,'reserved','unknown'):
            state = sent_state()
            if status is None:
                state['posts'].clear()
            else:
                state['posts'][POST['id']]['status'] = status
            http = Mock()
            with self.subTest(status=status), self.assertRaises(mw.WatchError):
                mw.refresh_preview(POST,state,http,WEBHOOK,NOW)
            http.json.assert_not_called()

    def test_edit_failure_is_not_retried_and_does_not_post(self):
        http = Mock()
        http.json.side_effect = [self.current_message(),mw.WatchError('timeout')]
        with self.assertRaises(mw.WatchError):
            mw.refresh_preview(POST,sent_state(),http,WEBHOOK,NOW)
        self.assertEqual([call.args[0] for call in http.json.call_args_list],['GET','PATCH'])

    def test_unconfirmed_edit_response_fails(self):
        for data in ({}, {'id':MESSAGE_ID,'flags':4,'embeds':[]},
                     {'id':MESSAGE_ID,'flags':0,'embeds':None},
                     {'id':MESSAGE_ID,'flags':0,'embeds':[]}):
            http = Mock()
            http.json.side_effect = [self.current_message(),data]
            with self.subTest(data=data), self.assertRaises(mw.WatchError):
                mw.refresh_preview(POST,sent_state(),http,WEBHOOK,NOW)

    def test_cli_requires_explicit_post_id(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mw.main(['refresh-preview'])


if __name__ == '__main__':
    unittest.main()
