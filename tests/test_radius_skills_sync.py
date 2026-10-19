"""Webhook sync of the managed Radius skills checkout, against local git repositories."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path


def git(*args, cwd=None):
    return subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def source_repo(path, skill):
    git("init", "-q", "-b", "main", str(path))
    return commit(path, skill)


def commit(path, skill):
    skill_dir = path / "plugins" / "radius" / "skills" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: " + skill + "\n---\n")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", skill, cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


class RadiusSkillsSyncTests(unittest.TestCase):
    def setUp(self):
        import scripts.agent_server.main as main

        self.main = main
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.archived = self.root / "skills"
        self.tools = self.root / "tools"
        self.skills_dir = self.root / "radius-skills"
        source_repo(self.archived, "radius-dev")
        self.tools_sha = source_repo(self.tools, "sell-api")
        for target, value in (
            ("RADIUS_SKILLS_DIR", str(self.skills_dir)),
            ("RADIUS_SKILLS_STAGING_ROOT", self.root / "staging"),
            ("_radius_skills_clone_url", lambda: str(self.tools)),
        ):
            patcher = unittest.mock.patch.object(main, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_checkout_of_another_repository_is_replaced_with_a_fresh_clone(self):
        git("clone", "-q", str(self.archived), str(self.skills_dir))

        self.main._sync_radius_skills_repo(self.tools_sha, "main")

        self.assertEqual(git("-C", str(self.skills_dir), "remote", "get-url", "origin"), str(self.tools))
        self.assertEqual(git("-C", str(self.skills_dir), "rev-parse", "HEAD"), self.tools_sha)
        self.assertTrue((self.skills_dir / "plugins/radius/skills/sell-api/SKILL.md").exists())
        self.assertFalse((self.skills_dir / "plugins/radius/skills/radius-dev").exists())

    def test_checkout_of_the_configured_repository_is_updated_in_place(self):
        git("clone", "-q", str(self.tools), str(self.skills_dir))
        marker = self.skills_dir / ".git" / "kept"
        marker.write_text("")
        new_sha = commit(self.tools, "x402")

        self.main._sync_radius_skills_repo(new_sha, "main")

        self.assertEqual(git("-C", str(self.skills_dir), "rev-parse", "HEAD"), new_sha)
        self.assertTrue((self.skills_dir / "plugins/radius/skills/x402/SKILL.md").exists())
        self.assertTrue(marker.exists())


class SameGitRemoteTests(unittest.TestCase):
    def test_ignores_git_suffix_and_trailing_slash(self):
        import scripts.agent_server.main as main

        url = "https://github.com/radiustechsystems/tools.git"
        self.assertTrue(main._same_git_remote("https://github.com/radiustechsystems/tools", url))
        self.assertTrue(main._same_git_remote("https://github.com/radiustechsystems/tools/", url))
        self.assertFalse(main._same_git_remote("https://github.com/radiustechsystems/skills.git", url))
        self.assertFalse(main._same_git_remote(None, url))


if __name__ == "__main__":
    unittest.main()
