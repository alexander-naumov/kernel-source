#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import print_function

import collections
import datetime
import os
import os.path
import pygit2
import re
import shelve
import shutil
import subprocess
import sys
import tempfile
import unittest

import git_sort
import lib


# from http://www.pygit2.org/recipes/git-show.html
class FixedOffset(datetime.tzinfo):
    """Fixed offset in minutes east from UTC."""

    def __init__(self, offset):
        self.__offset = datetime.timedelta(minutes = offset)

    def utcoffset(self, dt):
        return self.__offset

    def tzname(self, dt):
        return None # we don't know the time zone's name

    def dst(self, dt):
        return datetime.timedelta(0) # we don't know about DST


def format_patch(commit, tag):
    def format_sanitized_subject(message):
        """
        Reimplemented from the similarly named function in the git source.
        """
        def is_title_char(c):
            if ((c >= 'a' and c <= 'z') or (c >= 'A' and c <= 'Z') or
                (c >= '0' and c <= '9') or c == '.' or c == '_'):
                return True
            else:
                return False

        result = []
        space = False
        i = 0
        end = message.find("\n")
        if end == -1:
            end = len(message)
        while i < end:
            c = message[i]
            if is_title_char(c):
                if space and result:
                    result.append("-")
                result.append(c)
                space = False
                if c == ".":
                    while i + 1 < end and message[i + 1] == ".":
                        i = i + 1
            else:
                space = True
            i = i + 1
        return "".join(result[:52])
    name = format_sanitized_subject(commit.message) + ".patch"

    with open(name, mode="w") as f:
        f.write("From: %s <%s>\n" % (commit.author.name, commit.author.email,))
        tzinfo = FixedOffset(commit.author.offset)
        dt = datetime.datetime.fromtimestamp(float(commit.author.time), tzinfo)
        f.write("Date: %s\n" % (dt.strftime("%c %z"),))
        f.write("Patch-mainline: %s\n" % (tag,))
        f.write("Git-commit: %s\n" % (str(commit.id),))
        f.write("Subject: %s" % (commit.message,))
        if not commit.message.endswith("\n"):
            f.write("\n")
            if commit.message.find("\n") == -1:
                f.write("\n")
        else:
            if commit.message.count("\n") == 1:
                # ends with a newline but consists only of a subject.
                f.write("\n")
        f.write("---\n")
        args = []
        if len(commit.parents):
            args.append(commit.parents[0].tree)
        diff = commit.tree.diff_to_tree(*args, swap=True)
        f.write(diff.stats.format(pygit2.GIT_DIFF_STATS_FULL, width=79))
        f.write("\n")
        f.write(diff.patch)
        f.write("--\ngs-tests\n")

    return name


class TestQuiltMode(unittest.TestCase):
    def setUp(self):
        os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="gs_cache")

        # setup stub linux repository
        os.environ["LINUX_GIT"] = tempfile.mkdtemp(prefix="gs_repo")
        self.repo = pygit2.init_repository(os.environ["LINUX_GIT"])
        self.repo.config["user.email"] = "agraf@suse.de"
        self.repo.config["user.name"] = "Alexander Graf"
        readme_path = os.path.join(os.environ["LINUX_GIT"], "README")

        author = pygit2.Signature("Alice Author", "alice@authors.tld")
        committer = pygit2.Signature("Cecil Committer", "cecil@committers.tld")
        tree = self.repo.TreeBuilder()

        tree.insert("README", 
                    self.repo.create_blob("NAME = Roaring Lionus\n"),
                    pygit2.GIT_FILEMODE_BLOB)
        m0 = self.repo.create_commit(
            "refs/heads/mainline",
            author,
            committer,
            "Linux 4.9",
            tree.write(),
            []
        )
        self.m0 = m0
        self.repo.create_tag("v4.9", m0, pygit2.GIT_REF_OID, committer,
                             "Linux 4.9")

        tree.insert("README", 
                    self.repo.create_blob("NAME = Anniversary Edition\n"),
                    pygit2.GIT_FILEMODE_BLOB)
        m1 = self.repo.create_commit(
            "refs/heads/mainline",
            author,
            committer,
            "Linux 4.10-rc5",
            tree.write(),
            [m0]
        )
        self.m1 = m1
        self.repo.create_tag("v4.10-rc5", m1, pygit2.GIT_REF_OID, committer,
                             "Linux 4.10-rc5")

        author = pygit2.Signature('Alice Author', 'alice@authors.tld')
        tree.insert("driver.c", 
                    self.repo.create_blob("#include <linux/module.h>\n"),
                    pygit2.GIT_FILEMODE_BLOB)
        marc = pygit2.Signature("Marc Zyngier", "marc.zyngier@arm.com")
        m2 = self.repo.create_commit(
            "refs/heads/mainline",
            marc,
            marc,
            """KVM: arm/arm64: vgic-v3: Add accessors for the ICH_APxRn_EL2 registers

As we're about to access the Active Priority registers a lot more,
let's define accessors that take the register number as a parameter.

Tested-by: Alexander Graf <agraf@suse.de>
Acked-by: David Daney <david.daney@cavium.com>
Reviewed-by: Eric Auger <eric.auger@redhat.com>
Signed-off-by: Marc Zyngier <marc.zyngier@arm.com>
Signed-off-by: Christoffer Dall <cdall@linaro.org>
""",
            tree.write(),
            [m1]
        )
        self.m2 = m2

        tree.insert("README", 
                    self.repo.create_blob("NAME = Fearless Coyote\n"),
                    pygit2.GIT_FILEMODE_BLOB)
        m3 = self.repo.create_commit(
            "refs/heads/mainline",
            author,
            committer,
            "Linux 4.10-rc6",
            tree.write(),
            [m2]
        )
        self.repo.create_tag("v4.10-rc6", m3, pygit2.GIT_REF_OID, committer,
                             "Linux 4.10-rc6")

        self.repo.remotes.create("origin",
                            "git://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git")
        self.repo.references.create("refs/remotes/origin/master", m3)

        # setup stub kernel-source content
        self.ks_dir = tempfile.mkdtemp(prefix="gs_ks")
        k_org_canon_prefix = "git://git.kernel.org/pub/scm/linux/kernel/git/"
        patch_dir = os.path.join(self.ks_dir, "patches.suse")
        os.mkdir(patch_dir)
        os.chdir(patch_dir)
        m0_name = format_patch(self.repo.get(m0), "v4.9")
        m1_name = format_patch(self.repo.get(m1), "v4.10-rc5")
        open(os.path.join(self.ks_dir, "series.conf"), mode="w").write(
"""# Kernel patches configuration file

	########################################################
	# sorted patches
	########################################################
	patches.suse/%s
	patches.suse/%s

	########################################################
	# end of sorted patches
	########################################################
""" % (m0_name, m1_name,))

        ss_path = os.path.join(lib.libdir(), "series_sort.py")
        os.chdir(self.ks_dir)

        # This overlaps what is tested by test_series_sort, hence, not put in a
        # test of its own.
        subprocess.check_call([ss_path, "-c", "series.conf"])
        content = open("series.conf").read()
        output = subprocess.check_call([ss_path, "series.conf"])
        self.assertEqual(open("series.conf").read(), content)

        os.makedirs("tmp/current")
        os.chdir("tmp/current")
        subprocess.check_call(
            ["quilt", "setup", "--sourcedir", "../../", "../../series.conf"])

        #sys.stdin.readline()


    def tearDown(self):
        shutil.rmtree(os.environ["XDG_CACHE_HOME"])
        shutil.rmtree(os.environ["LINUX_GIT"])
        shutil.rmtree(self.ks_dir)


    def test_quilt_mode(self):
        qm_path = os.path.join(lib.libdir(), "quilt-mode.sh")

        # test series file replacement
        entries = ["%s\n" % (l,) for l in map(lambda line : line.strip(),
                                  open("series").readlines()) if l and not
                   l.startswith("#")]
        # remove the symlink
        os.unlink("series")
        open("series", mode="w").writelines(entries)
        subprocess.check_output(
            [os.path.join(lib.libdir(), "qgoto.py"), str(self.m0)])

        # test qgoto
        subprocess.check_output(". %s; qgoto %s" % (qm_path, str(self.m0)),
                              shell=True, executable="/bin/bash")

        # test qdupcheck
        try:
            result = subprocess.check_output(
                ". %s; qdupcheck %s" % (qm_path, str(self.m1)), shell=True,
                executable="/bin/bash")
        except subprocess.CalledProcessError as err:
            self.assertEqual(err.returncode, 1)
            self.assertEqual(err.output.splitlines()[-1].strip(),
                             "patches.suse/Linux-4.10-rc5.patch")
        else:
            self.assertTrue(False)
        
        subprocess.check_output(". %s; qgoto %s" % (qm_path, str(self.m1)),
                              shell=True, executable="/bin/bash")

        try:
            result = subprocess.check_output(
                ". %s; qdupcheck %s" % (qm_path, str(self.m1)), shell=True,
                executable="/bin/bash")
        except subprocess.CalledProcessError as err:
            self.assertEqual(err.returncode, 1)
            self.assertEqual(err.output.splitlines()[-1].strip(),
                             "This is the top patch.")
        else:
            self.assertTrue(False)

        # import m2
        subprocess.check_output(". %s; qgoto %s" % (qm_path, str(self.m2)),
                              shell=True, executable="/bin/bash")
        subprocess.check_output(
            """. %s; qcp -r "bsc#1077761" -d patches.suse %s""" % (
                qm_path, str(self.m2)), shell=True, executable="/bin/bash")

        retval = subprocess.check_output(("quilt", "--quiltrc", "-", "next",))
        name = "patches.suse/KVM-arm-arm64-vgic-v3-Add-accessors-for-the-ICH_APxR.patch"
        self.assertEqual(retval.strip(), name)

        try:
            retval = open(os.path.join(self.ks_dir, name)).readlines().index(
                "Acked-by: Alexander Graf <agraf@suse.de>\n")
        except ValueError:
            retval = -1
        self.assertNotEqual(retval, -1)

        retval = subprocess.check_output(("quilt", "--quiltrc", "-", "push",))

        try:
            result = subprocess.check_output(("quilt", "--quiltrc", "-",
                                              "pop",), stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as err:
            self.assertEqual(err.returncode, 1)
            self.assertTrue(err.output.endswith(
                "needs to be refreshed first.\n"))
        else:
            self.assertTrue(False)


if __name__ == '__main__':
    # Run a single testcase
    suite = unittest.TestLoader().loadTestsFromTestCase(TestQuiltMode)
    unittest.TextTestRunner(verbosity=2).run(suite)
