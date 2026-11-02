from __future__ import annotations
import json
import os
import queue
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from model_runner.runner import ModelRunner
from model_runner.credentials import CredentialManager
from model_runner.task_state import TaskRecord
from model_runner.core import VideoGenerationError
from product_swap import ProductSwapWorkflow
from media_inspection import VideoInfo


class Response:
    ok = True
    status_code = 200
    text = ''
    def __init__(self, payload=None, content=b'video-result'):
        self.payload = payload
        self.content = content
    def json(self):
        return self.payload


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.values = {'FAL_KEY': 'fake-private-value',
                       'FAL_API_BASE': 'https://queue.fal.run',
                       'DEEPSEEK_API_KEY': 'fake-vision-value'}
        self.runner = ModelRunner(ROOT, values=self.values)
        self.output = self.root/'result.mp4'
        self.args = Namespace(prompt='keep unchanged', provider=None, model='wan3',
                              reference=['image=https://example.com/product.png'],
                              output=str(self.output), duration=5, aspect_ratio='9:16',
                              resolution='480p', dry_run=False)
        self.record = TaskRecord.path_for(self.output)

    def test_network_loss_then_new_runner_resumes_same_task_without_post(self):
        first = Mock()
        first.post.return_value = Response({'request_id': 'task-one'})
        first.get.side_effect = ConnectionError('lost fake-private-value')
        with patch('requests.Session', return_value=first):
            with self.assertRaisesRegex(VideoGenerationError, '任务记录') as error:
                self.runner.run(self.args)
        self.assertNotIn('fake-private-value', str(error.exception))
        state = json.loads(self.record.read_text(encoding='utf-8'))
        self.assertEqual(state['task_id'], 'task-one')
        self.assertEqual(state['status'], 'interrupted')
        self.assertNotIn('fake-private-value', self.record.read_text(encoding='utf-8'))
        second = Mock()
        second.get.side_effect = [
            Response({'status': 'COMPLETED'}),
            Response({'video': {'url': 'https://v3b.fal.media/files/result.mp4'}}),
            Response(),
        ]
        with patch('requests.Session', return_value=second):
            result = ModelRunner(ROOT, values=self.values).resume(self.record)
        first.post.assert_called_once()
        second.post.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b'video-result')
        self.assertTrue(result['resumed_existing_task'])
        self.assertEqual(second.get.call_args.kwargs['headers'], {})
        with patch('requests.Session') as network:
            self.runner.resume(self.record)
        network.assert_not_called()

    def test_download_failure_resumes_download_only(self):
        session = Mock()
        session.post.return_value = Response({'request_id': 'task-one'})
        session.get.side_effect = [
            Response({'status': 'COMPLETED'}),
            Response({'video': {'url': 'https://v3b.fal.media/files/result.mp4'}}),
            ConnectionError('download offline fake-private-value'),
        ]
        with patch('requests.Session', return_value=session):
            result = self.runner.run(self.args)
        self.assertFalse(result['local_downloaded'])
        self.assertNotIn('fake-private-value', self.record.read_text(encoding='utf-8'))
        session.get.side_effect = None
        session.get.return_value = Response()
        session.reset_mock()
        with patch('requests.Session', return_value=session):
            result = self.runner.resume(self.record)
        self.assertTrue(result['local_downloaded'])
        session.post.assert_not_called()
        session.get.assert_called_once()
        self.assertEqual(session.get.call_args.args[0], 'https://v3b.fal.media/files/result.mp4')

    def test_unknown_submission_is_not_submitted_again(self):
        session = Mock()
        session.post.side_effect = ConnectionError('submission uncertain')
        with patch('requests.Session', return_value=session):
            with self.assertRaises(VideoGenerationError): self.runner.run(self.args)
            with self.assertRaisesRegex(VideoGenerationError, '已有任务'): self.runner.run(self.args)
            with self.assertRaisesRegex(VideoGenerationError, '核实'): self.runner.resume(self.record)
        session.post.assert_called_once()

    def test_ssl_preflight_failure_never_submits_or_uploads(self):
        import requests
        session = Mock()
        session.head.side_effect = requests.exceptions.SSLError('TLS EOF')
        with patch('requests.Session', return_value=session), patch('model_runner.adapters.fal_base.time.sleep'):
            with self.assertRaisesRegex(VideoGenerationError, '尚未提交'):
                self.runner.run(self.args)
        self.assertEqual(session.head.call_count, 3)
        session.post.assert_not_called()
        session.put.assert_not_called()
        self.assertEqual(json.loads(self.record.read_text(encoding='utf-8'))['status'], 'not_submitted')

    def test_ssl_submission_is_unknown_and_not_retried(self):
        import requests
        session = Mock()
        session.post.side_effect = requests.exceptions.SSLError('TLS EOF')
        with patch('requests.Session', return_value=session):
            with self.assertRaisesRegex(VideoGenerationError, '没有任务编号，无法使用') as error:
                self.runner.run(self.args)
        session.post.assert_called_once()
        self.assertNotIn('使用 --resume', str(error.exception))
        self.assertEqual(json.loads(self.record.read_text(encoding='utf-8'))['status'], 'submission_unknown')

    def test_ssl_preflight_and_query_recover_without_duplicate_post(self):
        import requests
        session = Mock()
        session.head.side_effect = [requests.exceptions.SSLError('TLS EOF'), Mock()]
        session.post.return_value = Response({'request_id': 'task-one'})
        session.get.side_effect = [requests.exceptions.SSLError('TLS EOF'),
                                  Response({'status': 'COMPLETED'}),
                                  requests.exceptions.SSLError('TLS EOF'),
                                  Response({'video': {'url': 'https://media.example/out.mp4'}}), Response()]
        with patch('requests.Session', return_value=session), patch('model_runner.adapters.fal_base.time.sleep'):
            result = self.runner.run(self.args)
        self.assertTrue(result['local_downloaded'])
        session.post.assert_called_once()
        self.assertEqual(session.head.call_count, 2)

    def test_terminal_failure_cannot_be_resubmitted_by_resume(self):
        session = Mock()
        session.post.return_value = Response({'request_id': 'failed-task'})
        session.get.return_value = Response({'status': 'failed', 'error': 'rejected'})
        with patch('requests.Session', return_value=session):
            with self.assertRaises(VideoGenerationError): self.runner.run(self.args)
            with self.assertRaisesRegex(VideoGenerationError, '明确失败'): self.runner.resume(self.record)
        session.post.assert_called_once()
        self.assertEqual(json.loads(self.record.read_text(encoding='utf-8'))['status'], 'failed')

    def test_different_provider_base_is_rejected_on_resume(self):
        session = Mock()
        session.post.return_value = Response({'request_id': 'task-one'})
        session.get.side_effect = ConnectionError('offline')
        with patch('requests.Session', return_value=session):
            with self.assertRaises(VideoGenerationError): self.runner.run(self.args)
        changed = ModelRunner(ROOT, values={**self.values, 'FAL_API_BASE': 'https://different.example.com'})
        with self.assertRaisesRegex(VideoGenerationError, '原任务不一致'): changed.resume(self.record)

    def test_existing_task_stops_product_analysis_before_charging(self):
        source, image = self.root/'source.mp4', self.root/'product.png'
        source.write_bytes(b'video'); image.write_bytes(b'image')
        self.record.write_text('{}')
        with patch('product_swap.probe_video', return_value=VideoInfo(8, 160, 90, '16:9')), patch('prompt_engine.analyze_video_and_relation') as vision:
            with self.assertRaisesRegex(VideoGenerationError, '已有任务记录'):
                ProductSwapWorkflow(self.runner).generate(source, image, '大小一致', self.output)
        vision.assert_not_called()

    def test_concurrent_call_cannot_take_same_record(self):
        with TaskRecord(self.record).lock():
            with self.assertRaisesRegex(VideoGenerationError, '另一个调用'):
                with TaskRecord(self.record).lock(): pass


class ConfigurationTests(unittest.TestCase):
    def test_two_skill_directories_ignore_process_and_cwd_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            one, two = root/'one', root/'two'
            one.mkdir(); two.mkdir()
            (one/'.env').write_text('FAL_KEY=one')
            (two/'.env').write_text('FAL_KEY=two')
            (root/'.env').write_text('FAL_KEY=wrong-cwd')
            providers = ModelRunner(ROOT, values={}).registry.providers
            with patch.dict(os.environ, {'FAL_KEY': 'wrong-env'}), patch('pathlib.Path.cwd', return_value=root):
                before = dict(os.environ)
                first = CredentialManager(one, providers).resolve('fal')
                second = CredentialManager(two, providers).resolve('fal')
                self.assertEqual(dict(os.environ), before)
            self.assertEqual(first.api_key, 'one')
            self.assertEqual(second.api_key, 'two')

    def test_host_snapshot_is_independent_and_never_reads_env_files(self):
        values = {'FAL_KEY': 'owned'}
        with patch('model_runner.credentials.load_env') as read:
            runner = ModelRunner(ROOT, values=values)
        values['FAL_KEY'] = 'changed'
        self.assertEqual(runner.credentials.resolve('fal').api_key, 'owned')
        read.assert_not_called()


class DesktopWorkflowTests(unittest.TestCase):
    def test_desktop_executor_calls_shared_workflow_with_explicit_settings(self):
        from desktop_tasks import execute_job
        values = {'source': 'source.mp4', 'image': 'product.png', 'relation': '一样大',
                'output': 'result.mp4', 'model': 'wan3.0-video', 'duration': 8, 'aspect_ratio': '16:9',
                'resolution': '480p', 'vision_model': 'chosen-vision'}
        store = Mock()
        store.import_asset.side_effect = lambda p: {'path': p, 'id': p}
        with patch('product_swap.ProductSwapWorkflow') as workflow:
            workflow.return_value.generate.return_value = {'success': True}
            execute_job({'kind': 'full', 'config': values}, {}, Mock(), store)
        workflow.return_value.generate.assert_called_once()
        kwargs = workflow.return_value.generate.call_args.kwargs
        self.assertEqual((kwargs['model'], kwargs['duration'], kwargs['resolution']), ('wan3.0-video', 8, '480p'))
        self.assertEqual(kwargs['vision_model'], 'chosen-vision')


if __name__ == '__main__':
    unittest.main()
