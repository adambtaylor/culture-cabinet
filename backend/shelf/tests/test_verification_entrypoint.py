"""Exercise supported CLI without ROOT/secret/settings injection."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from django.test import SimpleTestCase
from cabinet import boundary


class VerificationEntrypointTests(SimpleTestCase):
    def run_cli(self,args,*,root=None,extra=None):
        environment=dict(os.environ)
        environment.pop('DJANGO_SETTINGS_MODULE',None)
        if extra: environment.update(extra)
        root=root or boundary.ROOT
        return subprocess.run([sys.executable,'-B',str(root/'backend/manage.py'),*args],
            cwd=root,env=environment,capture_output=True,text=True,timeout=20)

    def test_F15_supported_direct_check_dryrun_without_store(self):
        self.assertEqual(boundary.ROOT,Path(__file__).resolve().parents[3])
        self.assertNotEqual(boundary.ROOT,boundary.DEPLOYMENT_ROOT)
        self.assertFalse(boundary.STORE.exists())
        for args in (['check'],['makemigrations','--check','--dry-run']):
            result=self.run_cli(args)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse(boundary.STORE.exists())

    def test_F15_candidate_runtime_commands_refused_before_store_or_secret(self):
        for command in ('demo_migrate','demo_bootstrap','demo_runserver'):
            result=self.run_cli([command,'--synthetic-demo'])
            self.assertEqual(result.returncode,1)
            self.assertEqual(result.stderr,'Refused: fixed synthetic-demo configuration required.\n')
            self.assertEqual(result.stdout,'')
        self.assertFalse(boundary.STORE.exists())

    def test_F15_actual_private_names_options_and_nonentrypoint_fail(self):
        for args,extra in ((['check'],{'CC_MODE':'fiction'}),
            (['check','--settings=private.settings'],{}),(['check','--pythonpath=/private'],{})):
            result=self.run_cli(args,extra=extra)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(result.stderr,'Refused: fixed synthetic-demo configuration required.\n')
        environment=dict(os.environ); environment.pop('DJANGO_SETTINGS_MODULE',None)
        result=subprocess.run([sys.executable,'-B','-c','import cabinet.settings'],cwd=boundary.ROOT/'backend',
            env=environment,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Use backend/manage.py',result.stderr)

    def test_F15_isolated_source_copy_rejects_path_attacks_and_keeps_store_absent(self):
        with TemporaryDirectory(prefix='source-verification-') as folder:
            root=Path(folder); shutil.copytree(boundary.ROOT/'backend',root/'backend',ignore=shutil.ignore_patterns('__pycache__'))
            (root/'static').mkdir()
            self.assertEqual(self.run_cli(['check'],root=root).returncode,0)
            self.assertFalse((root/'.demo-data').exists())
            store=root/'.demo-data'; store.mkdir()
            database=store/'synthetic.sqlite3'; key=store/'session-key'
            fixture=root/'fictional-canary'; fixture.write_text('fictional only')
            for target in (database,key):
                target.symlink_to(root/'missing-fiction')
                self.assertNotEqual(self.run_cli(['check'],root=root).returncode,0)
                target.unlink()
                os.link(fixture,target)
                self.assertNotEqual(self.run_cli(['check'],root=root).returncode,0)
                target.unlink(); target.mkdir()
                self.assertNotEqual(self.run_cli(['check'],root=root).returncode,0)
                target.rmdir()
            store.rmdir(); store.symlink_to(root/'missing-store',target_is_directory=True)
            self.assertNotEqual(self.run_cli(['check'],root=root).returncode,0)
