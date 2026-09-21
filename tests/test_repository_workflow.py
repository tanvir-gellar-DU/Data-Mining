import json
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.data_mining import parse_args, run
from src.storage import write_jsonl
from src.build_analysis_dataset import build as real_build


class RepositoryWorkflowTests(unittest.TestCase):
    def setup_args(self, root):
        repos = root / "repos.txt"
        repos.write_text("owner/one\nowner/two\n")
        return parse_args(["--repos", str(repos), "--mining-output", str(root / "mining"),
                           "--output", str(root / "final"), "--skip-actions-enrichment"])

    def collector(self, events):
        def collect(client, repo, output, *args):
            events.append(("collect", repo))
            meta = dict(repository=repo, default_branch="main", head_sha="a", processed_at="now",
                        collection_start=None, collection_cutoff=None)
            write_jsonl(output / "repository_metadata" / (repo.replace('/', '__') + '.jsonl'), [meta])
            return meta, []
        return collect

    def test_cleanup_after_validation_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args = self.setup_args(root); events = []
            def build(ns):
                events.append(("build", ns.repository))
                cache = ns.input / '.git-cache' / (ns.repository.replace('/', '__') + '.git')
                cache.mkdir(parents=True)
                result = real_build(ns)
                self.assertTrue((ns.output / 'validation_summary.json').exists())
                return result
            with patch('src.repository_workflow.collect_repository', side_effect=self.collector(events)), \
                 patch('src.repository_workflow.build', side_effect=build):
                self.assertEqual(0, run(args))
                self.assertEqual([('collect','owner/one'),('build','owner/one'),
                                  ('collect','owner/two'),('build','owner/two')], events)
                self.assertEqual([], list((args.mining_output / '.git-cache').iterdir()))
                events.clear()
                self.assertEqual(0, run(args))
                self.assertEqual([('collect','owner/one'),('collect','owner/two')], events)
                summary=json.loads((args.output / 'validation_summary.json').read_text())
                self.assertEqual(2, summary['processed_repositories'])
                self.assertFalse(summary['partial'])

    def test_interrupted_build_keeps_clone_and_no_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); args=self.setup_args(root)
            cache=args.mining_output / '.git-cache/owner__one.git'
            def interrupted(ns):
                cache.mkdir(parents=True)
                raise KeyboardInterrupt()
            with patch('src.repository_workflow.collect_repository', side_effect=self.collector([])), \
                 patch('src.repository_workflow.build', side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt): run(args)
            self.assertTrue(cache.exists())
            self.assertFalse((args.output / 'repositories/owner__one/complete.json').exists())

    def test_two_repository_nonempty_csvs_are_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); args=self.setup_args(root)
            def collect(client, repo, output, *unused):
                rows=[]
                for number, conclusion in enumerate(['success','failure','success'], 1):
                    rows.append(dict(repository=repo, workflow_id=1, branch='main', workflow_name='CI',
                        head_sha='a', conclusion=conclusion, run_id=number,
                        created_at=f'2026-04-0{number}T00:00:00Z', html_url='https://example.invalid'))
                return {}, rows
            def enrich(client, episodes, output, *unused, **kwargs):
                from src.storage import read_jsonl
                records=read_jsonl(output/'commit_changes/commits.jsonl')
                records.append(dict(repository=episodes[0]['repository'],commit_sha='a',parent_sha='p',files=[]))
                write_jsonl(output/'commit_changes/commits.jsonl',records)
                return []
            with patch('src.repository_workflow.collect_repository', side_effect=collect), \
                 patch('src.repository_workflow.enrich', side_effect=enrich), \
                 patch('src.build_analysis_dataset.ActionsEnricher.enrich', return_value=({'Failed Job':'','Failed Step':'','Error Summary':''}, [])):
                self.assertEqual(0,run(args))
            with (args.output/'episodes.csv').open() as f: erows=list(csv.DictReader(f))
            with (args.output/'attempts.csv').open() as f: arows=list(csv.DictReader(f))
            self.assertEqual(2,len(erows)); self.assertEqual(2,len(arows))
            self.assertEqual({'owner/one','owner/two'},{r['Repository'] for r in erows})
            self.assertEqual({r['Episode ID'] for r in erows},{r['Episode ID'] for r in arows})
