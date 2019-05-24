#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright © 2015-2018 ANSSI. All Rights Reserved.
#
# Helpers to automate Portage tree management.
#
# Author: Mickaël Salaün <clipos@ssi.gouv.fr>

# The only place where the version of this Python package is defined (setup.py
# reparses only this line for setuptools). This versioning follows semver.
__version__ = "0.1.0"

import logging
import os
import shutil

import portage

PORTAGE_ARCH = "amd64"

DRY_RUN = False

def get_atom_dependencies(atom, db=None, all_useflags=False):
    if db is None:
        db = portage.db[portage.root]["porttree"].dbapi
    if atom != "":
        return portage.dep.use_reduce(portage.dep.paren_enclose(db.aux_get(atom, ["DEPEND", "RDEPEND"])), matchall=all_useflags)
    return []

class PkgFound(str):
    pass

class PkgNotFound(str):
    pass

class OutsideOfPortageTreeException(Exception):
    pass

def get_all_dependencies(depends, db=None, pkgs=None, all_useflags=False, exclude=set()):
    if db is None:
        db = portage.db[portage.root]["porttree"].dbapi
    if pkgs is None:
        pkgs = PackageDb(None)
    for dep in depends:
        if isinstance(dep, list):
            for sub in get_all_dependencies(dep, db, pkgs, all_useflags, exclude):
                yield sub
            continue
        if dep.startswith("!"):
            continue
        if dep == u"||":
            continue
        pkgs_deps = pkgs.match(dep)
        if pkgs_deps:
            # Assume we have all the dependencies (i.e. use flags) for our packages
            exclude.update([PkgFound(p) for p in pkgs_deps])
            continue
        atom = db.xmatch("bestmatch-visible", dep)
        if atom == "":
            atom = PkgNotFound(dep)
        else:
            atom = PkgFound(atom)
        if atom in exclude:
            continue
        exclude.add(atom)
        yield atom
        if isinstance(atom, PkgNotFound):
            continue
        for sub in get_all_dependencies(get_atom_dependencies(atom, db, all_useflags), db, pkgs, all_useflags, exclude):
            yield sub

class MultiDb(object):
    """wrapper around a set of Portage databases"""
    def __init__(self, portdir, profiles):
        self.portdir = os.path.abspath(portdir)
        if not os.path.exists(os.path.join(self.portdir, "metadata")):
            raise Exception("Portage tree is not valid: {}".format(self.portdir))
        # TODO: find a better way to check that profile is effectively a
        # profile path ("eapi" file is not mandatory)
        #if not os.path.exists(os.path.join(profile, "eapi")):
        #    raise Exception("Profile is not valid: {}".format(profile))
        os.environ["PORTDIR"] = self.portdir
        # PORTAGE_CONFIGROOT: Virtual root to find configuration (e.g. etc/make.conf)
        #os.environ["PORTAGE_CONFIGROOT"] = WORKDIR
        portage.const.PROFILE_PATH = ""
        self.configs = [portage.config(config_profile_path=os.path.abspath(p)) for p in profiles]
        self._db = portage.db[portage.root]["porttree"].dbapi
        # ignore overlays
        self._db.porttrees = [ self.portdir ]

    def _get_dbs(self):
        for config in self.configs:
            self._db.settings = config
            yield self._db

    # get the atom path which is beneath the selected portdir (there is only
    # one portdir, no overlay)
    def get_atom_path_selected(self, mycpv, mytree=None, myrepo=None):
        for db in self._get_dbs():
            path = db.findname2(mycpv, mytree, myrepo)[0]
            if path is not None:
                # same semantic as assert_beneath_portdir(path)
                assert os.path.commonprefix([self.portdir, path]) == self.portdir
                return path
        return None

    def get_atom_dir_selected(self, mycpv, mytree=None, myrepo=None):
        path = self.get_atom_path_selected(mycpv, mytree, myrepo)
        if path is not None:
            return os.path.dirname(path)
        return None

    def cp_all(self, categories=None, trees=None):
        ret = set()
        for db in self._get_dbs():
            ret.update(db.cp_all(categories, trees))
        return ret

    def aux_get_first(self, mycpv, mylist, mytree=None, myrepo=None):
        for db in self._get_dbs():
            ret = db.aux_get(mycpv, mylist, mytree, myrepo)
            if ret:
                return ret
        return []

    def match(self, mydep, use_cache=1):
        ret = set()
        for db in self._get_dbs():
            # Matches which are not part of the selected portdir are ignored
            # thanks to the initial porttrees filtering.
            ret.update(db.match(mydep, use_cache))
        return ret

    def match_all(self, mycpv):
        ret = set()
        for db in self._get_dbs():
            # "match-all" "bestmatch-visible" "match-visible" "minimum-all" "list-visible"
            ret.update(db.xmatch("match-all", mycpv))
        return ret

    def match_visibles(self, mycpv):
        ret = set()
        for db in self._get_dbs():
            ret.update(db.xmatch("list-visible", mycpv))
        return ret

    def match_best_visibles(self, mycpv):
        ret = set()
        for db in self._get_dbs():
            m = db.xmatch("bestmatch-visible", mycpv)
            if len(m) != 0:
                ret.add(m)
        return ret

def set_stable(db, is_stable):
    db.settings.unlock()
    db.settings["ACCEPT_KEYWORDS"] = "{}{}".format({True:"", False:"~"}[is_stable], PORTAGE_ARCH);
    db.settings.lock()

def assert_beneath_portdir(src):
    # $PORTDIR is set by MultiDb
    portdir = os.environ["PORTDIR"]
    root = "{}/".format(portdir)
    if len(portdir) > 1 and os.path.commonprefix([root, src]) == root:
        return
    raise OutsideOfPortageTreeException("Attempt to modify a file outside the Portage tree: {}", src)

def fs_move(common_dir, src, dst):
    src = os.path.join(common_dir, src)
    dst = os.path.join(common_dir, dst)
    assert_beneath_portdir(src)
    assert_beneath_portdir(dst)
    logging.debug("moving {} -> {}".format(src, dst))
    if not DRY_RUN:
        shutil.move(src, dst)

def fs_symlink(common_dir, src, dst):
    src = os.path.join(common_dir, src)
    assert_beneath_portdir(src)
    if dst != os.path.basename(dst):
        raise Exception("Attempt to symlink to an absolute path: {}", dst)
    logging.debug("linking {} -> {}".format(src, dst))
    if not DRY_RUN:
        os.symlink(dst, src)

def fs_remove(src):
    assert_beneath_portdir(src)
    logging.debug("removing file {}".format(src))
    if not DRY_RUN:
        os.unlink(src)

def fs_remove_tree(src):
    assert_beneath_portdir(src)
    logging.debug("removing tree {}".format(src))
    if not DRY_RUN:
        shutil.rmtree(src)

def do_symlinks(mdb, slots, atom, atom_dir):
    logging.debug("working in {}".format(atom_dir))
    # for each slot, get the best match according to the profile (i.e. newer stable, or newer unstable if the profile whitelist this atom)
    visibles = set()
    for slot in slots:
        for cpv in mdb.match_best_visibles("{}:{}".format(atom, slot)):
            slot, keywords = mdb.aux_get_first(cpv, ["SLOT", "KEYWORDS"])
            visibles.add(os.path.basename(cpv))
            # may use isStable() from portage/package/ebuild/_config/KeywordsManager.py
            logging.debug("found {} slot:{}".format(cpv, slot))
    for root, dirs, files in os.walk(atom_dir):
        for name in files:
            head, tail = os.path.splitext(name)
            # remove invisible ebuilds
            if tail == ".ebuild" and head not in visibles:
                try:
                    fs_remove(os.path.join(atom_dir, name))
                except OutsideOfPortageTreeException as exc:
                    logging.warning("skipping file for deletion (duplicate atoms?): %s/%s", atom_dir, name)
                    continue

    # It's more common to remove an old package version than the more
    # up-to-date (i.e. we don't downgrade packages but can keep and old version
    # for compatibility): decrease order to keep the most up to date at first.
    for i, pvr in enumerate(sorted([portage.pkgsplit(x) for x in visibles], portage.pkgcmp, reverse=True)):
        if pvr[2] == "r0":
            name = "-".join(pvr[:-1])
        else:
            name = "-".join(pvr)
        src = "{}.ebuild".format(name)

        # ignore already equalized ebuilds
        if os.path.islink(os.path.join(atom_dir, src)):
            continue

        # the equalized name should not end with ".ebuild"
        # (cf. dbapi/porttree.py:cp_list "Invalid ebuild name" and
        # versions.py:catpkgsplit)
        dst = ".{}.ebuild.{}".format(pvr[0], i)
        try:
            fs_move(atom_dir, src, dst)
        except OutsideOfPortageTreeException as exc:
            logging.warning("skipping atom for move (duplicate atoms?): %s/(%s -> %s)", atom_dir, src, dst)
            continue
        try:
            fs_symlink(atom_dir, src, dst)
        except OutsideOfPortageTreeException as exc:
            logging.warning("skipping atom for symlink (duplicate atoms?): %s/(%s -> %s)", atom_dir, src, dst)
            continue

def equalize(mdb, atoms=None, dry_run=False):
    """Equalize a Gentoo Portage tree. If `atoms` is None (or emtpy list, or
    whatever evaluates to False), then this function will equalize the entire
    Portage tree.

    :param mdb: a set of Portage tree databases from MultiDb
    """

    global DRY_RUN
    DRY_RUN = dry_run

    # get the entire list of atoms provided by the set of Portage tree
    # databases `mdb`
    if not atoms:
        atoms = mdb.cp_all()

    atom_nb = len(atoms)
    for i, atom in enumerate(atoms, start=1):
        # find all the slots for this atom
        slots = set()
        for cpv in mdb.match(atom):
            slots.add(mdb.aux_get_first(cpv, ["SLOT"])[0])
        logging.debug("")
        logging.info("equalizing {}/{} {}".format(i, atom_nb, atom))
        # check if some slots are visible to the current profile
        if len(slots) != 0:
            do_symlinks(mdb, slots, atom, mdb.get_atom_dir_selected(cpv))
            continue
        # remove files which are not usable with the current profile
        cpvs = mdb.match_all(atom)
        if len(cpvs) == 0:
            raise Exception("Missing atom in the cache, you should run `egencache --update` for this Portage tree")
        atom_dir = mdb.get_atom_dir_selected(cpvs.pop())
        try:
            fs_remove_tree(atom_dir)
        except OutsideOfPortageTreeException as exc:
            logging.debug("skipping atom dir for deletion: %s", atom_dir)
            continue
