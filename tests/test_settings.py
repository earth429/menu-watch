import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import menu_watch as mw
from scripts.update_schedule import sync_schedule

NOW = mw.parse_time('2026-10-21T14:00:00+09:00')
FIXTURE = Path(__file__).parent / 'fixtures/bampire/timeline-2026-10-21.json'
EXAMPLE = next(post for post in json.loads(FIXTURE.read_text())['results']
               if post['id'] == '2112689033075929439')


class SettingsTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text(text, encoding='utf-8')
            return mw.load_settings(path)

    def test_project_defaults_daily_in_jst(self):
        settings = mw.load_settings()
        self.assertEqual(settings, mw.DEFAULT_SETTINGS)
        self.assertEqual(settings.cron_utc, '17 6 * * *')

    def test_schedule_converts_early_morning_and_hourly(self):
        self.assertEqual(self.load('[schedule]\nhour_jst=0\nminute=5').cron_utc, '5 15 * * *')
        self.assertEqual(self.load('[schedule]\nfrequency="hourly"').cron_utc, '17 * * * *')

    def test_unknown_invalid_and_out_of_range_values_fail(self):
        for text in ('[watc]\nmax_pages=2', '[watch]\nmax_page=2',
                     'watch=1', '[watch]\nscope="stores"',
                     '[watch]\ninclude_revivals="false"',
                     '[watch]\nmax_pages=true', '[watch]\nmax_pages=0',
                     '[watch]\nmax_pages=6', '[watch]\nmax_notifications=4',
                     '[watch]\nlookback_hours=0', '[watch]\nlookback_hours=169',
                     '[schedule]\nfrequency="weekly"', '[schedule]\nhour_jst=24',
                     '[schedule]\nminute=-1', '[notification]\nstyle="missing"',
                     '[notification]\nlink_preview="yes"',
                     'bad toml !'):
            with self.subTest(text=text), self.assertRaises(mw.WatchError):
                self.load(text)

    def test_custom_limits_apply_to_fetch_and_candidates(self):
        settings = self.load('[watch]\nmax_pages=1\nmax_notifications=1\nlookback_hours=72')
        http = Mock()
        http.json.return_value = {'code':200, 'results':[EXAMPLE], 'cursor':{'bottom':'next'}}
        self.assertEqual(mw.fetch_posts(http, settings), [EXAMPLE])
        self.assertEqual(http.json.call_count, 1)
        old = copy.deepcopy(EXAMPLE)
        old['created_timestamp'] = int((NOW-timedelta(hours=60)).timestamp())
        old['id'] = '123456789'
        selected, _ = mw.candidates([EXAMPLE, old], None, NOW, settings)
        self.assertEqual([post.id for post in selected], [old['id']])
        self.assertEqual(mw.parse_menu(old, NOW, NOW-timedelta(days=7))[1], 'outside-window')

    def test_revival_filter_applies_and_keeps_regular_menus(self):
        settings = self.load('[watch]\ninclude_revivals=false')
        for note in ('復活！', 'リターン！', '戻ってきました！', '帰ってきました！'):
            revived = copy.deepcopy(EXAMPLE)
            revived['text'] += '\n\n' + note
            parsed, reason = mw.parse_menu(revived, NOW, NOW-timedelta(days=1), settings)
            self.assertIsNone(parsed)
            self.assertEqual(reason, 'revivals-disabled')
        self.assertIsNotNone(mw.parse_menu(EXAMPLE, NOW, NOW-timedelta(days=1), settings)[0])

    def test_original_message_is_escaped_and_has_canonical_link(self):
        settings = self.load('[notification]\nstyle="original"')
        parsed, _ = mw.parse_menu(EXAMPLE, NOW, NOW-timedelta(days=1), settings)
        content = mw.format_message(parsed, settings)
        self.assertIn('本日オンメニューは', content)
        self.assertIn('ヤゲン軟骨', content)
        self.assertTrue(content.endswith('https://x.com/bampirePR/status/'+EXAMPLE['id']))
        self.assertNotIn('本日オンメニューは', mw.format_message(parsed))

    def test_original_style_length_is_checked_before_delivery(self):
        long_post = copy.deepcopy(EXAMPLE)
        long_post['text'] += '\n\n' + '長い説明です。'*350
        parsed, reason = mw.parse_menu(long_post, NOW, NOW-timedelta(days=1),
                                      replace(mw.DEFAULT_SETTINGS, message_style='original'))
        self.assertIsNone(parsed)
        self.assertEqual(reason, 'message-too-long')
        self.assertIsNotNone(mw.parse_menu(long_post, NOW, NOW-timedelta(days=1))[0])

    def test_invalid_config_stops_before_any_io(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('[watch]\nmax_page=2', encoding='utf-8')
            with patch.object(mw, 'HTTP') as http, patch.object(mw, 'GitHubState') as store:
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(mw.main(['notify','--config',str(path)]), 1)
                http.assert_not_called()
                store.assert_not_called()


class ScheduleTests(unittest.TestCase):
    def test_schedule_is_in_sync_in_repository(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(sync_schedule(root/'menu-watch.toml',
                                      root/'.github/workflows/notify-bampire.yml', check=True),
                         mw.load_settings().cron_utc)

    def test_edit_updates_workflow_and_check_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            config, workflow = Path(directory)/'config.toml', Path(directory)/'workflow.yml'
            config.write_text('[schedule]\nfrequency="daily"\nhour_jst=20\nminute=30')
            original = "on:\n  schedule:\n    - cron: '17 * * * *'\n"
            workflow.write_text(original)
            with self.assertRaises(mw.WatchError):
                sync_schedule(config, workflow, check=True)
            self.assertEqual(workflow.read_text(), original)
            sync_schedule(config, workflow)
            self.assertIn("cron: '30 11 * * *'", workflow.read_text())
            self.assertEqual(sync_schedule(config, workflow, check=True), '30 11 * * *')

    def test_missing_or_multiple_cron_entries_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config, workflow = Path(directory)/'config.toml', Path(directory)/'workflow.yml'
            config.write_text('')
            for content in ('on: {}\n', "  - cron: '0 * * * *'\n  - cron: '30 * * * *'\n"):
                workflow.write_text(content)
                with self.assertRaises(mw.WatchError):
                    sync_schedule(config, workflow)
                self.assertEqual(workflow.read_text(), content)


if __name__ == '__main__':
    unittest.main()
