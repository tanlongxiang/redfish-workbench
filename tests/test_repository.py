"""Repository hygiene and path isolation; no site files or real network requests."""
from pathlib import Path
import subprocess,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'core'))
from workspace_paths import WORKSPACE,RUNS_DIR,SOURCE_DIR,FINAL_DIR,ARCHIVE_DIR,PACKAGE_DIR,default_inputs
from local_config import ENV_FILE
class RepositoryTests(unittest.TestCase):
    def test_paths_do_not_escape_into_old_workspace(self):
        self.assertEqual(WORKSPACE,ROOT)
        for path in [RUNS_DIR,SOURCE_DIR,FINAL_DIR,ARCHIVE_DIR,*default_inputs()]:self.assertTrue(path.is_relative_to(ROOT/'var'))
        self.assertEqual(PACKAGE_DIR,ROOT/'dist')
        self.assertEqual(ENV_FILE,ROOT/'.env')
    def test_ignore_rules_protect_runtime_but_allow_template(self):
        for name in ['.env','core/.env','var/web/portal.sqlite3','dist/release.zip','web/frontend/node_modules/a.js','docs/reference/G50.pdf','site_nodes.csv','firmware.jed','access-key']:
            result=subprocess.run(['git','-C',str(ROOT),'check-ignore','--no-index','-q',name])
            self.assertEqual(result.returncode,0,name)
        self.assertEqual(subprocess.run(['git','-C',str(ROOT),'check-ignore','--no-index','-q','.env.example']).returncode,1)
    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        subprocess.run(['git','init','-q','-b','main',str(root)],check=True)
        (root/'scripts').mkdir();(root/'scripts/security_check.py').write_bytes((ROOT/'scripts/security_check.py').read_bytes())
        return root
    def scan(self,root):return subprocess.run([sys.executable,'-I','-B',str(root/'scripts/security_check.py')],capture_output=True,text=True)
    def test_index_not_worktree_is_scanned(self):
        root=self.fixture();(root/'note.txt').write_text('-----BEGIN '+'PRIVATE KEY-----\ninert fixture\n')
        subprocess.run(['git','-C',str(root),'add','note.txt'],check=True)
        (root/'note.txt').write_text('worktree was cleaned, but index still contains the fixture')
        result=self.scan(root);self.assertEqual(result.returncode,1);self.assertIn('private-key',result.stderr);self.assertNotIn('inert fixture',result.stderr)
    def test_valid_empty_template(self):
        root=self.fixture();(root/'.env.example').write_text('BMC_USERNAME=\nBMC_PASSWORD=\n')
        subprocess.run(['git','-C',str(root),'add','.env.example'],check=True)
        self.assertEqual(self.scan(root).returncode,0)
    def test_nonempty_template_is_blocked_without_echo(self):
        root=self.fixture();secret='fixture-only-not-real';(root/'.env.example').write_text('BMC_PASSWORD='+secret+'\n')
        subprocess.run(['git','-C',str(root),'add','.env.example'],check=True)
        result=self.scan(root);self.assertEqual(result.returncode,1);self.assertNotIn(secret,result.stderr)
    def test_field_file_is_blocked_even_if_forced_into_index(self):
        root=self.fixture();(root/'field.xlsx').write_bytes(b'inert')
        subprocess.run(['git','-C',str(root),'add','field.xlsx'],check=True)
        self.assertEqual(self.scan(root).returncode,1)
if __name__=='__main__':unittest.main()
