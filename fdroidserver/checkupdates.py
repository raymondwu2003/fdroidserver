#!/usr/bin/env python2
# -*- coding: utf-8 -*-
#
# checkupdates.py - part of the FDroid server tools
# Copyright (C) 2010-2015, Ciaran Gultnieks, ciaran@ciarang.com
# Copyright (C) 2013-2014 Daniel Martí <mvdan@mvdan.cc>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sys
import os
import re
import urllib2
import time
import subprocess
from argparse import ArgumentParser
import traceback
import HTMLParser
from distutils.version import LooseVersion
import logging
import copy

import common
import metadata
from common import VCSException, FDroidException
from metadata import MetaDataException


# Check for a new version by looking at a document retrieved via HTTP.
# The app's Update Check Data field is used to provide the information
# required.
def check_http(app):

    try:

        if not app.UpdateCheckData:
            raise FDroidException('Missing Update Check Data')

        urlcode, codeex, urlver, verex = app.UpdateCheckData.split('|')

        vercode = "99999999"
        if len(urlcode) > 0:
            logging.debug("...requesting {0}".format(urlcode))
            req = urllib2.Request(urlcode, None)
            resp = urllib2.urlopen(req, None, 20)
            page = resp.read()

            m = re.search(codeex, page)
            if not m:
                raise FDroidException("No RE match for version code")
            vercode = m.group(1)

        version = "??"
        if len(urlver) > 0:
            if urlver != '.':
                logging.debug("...requesting {0}".format(urlver))
                req = urllib2.Request(urlver, None)
                resp = urllib2.urlopen(req, None, 20)
                page = resp.read()

            m = re.search(verex, page)
            if not m:
                raise FDroidException("No RE match for version")
            version = m.group(1)

        return (version, vercode)

    except FDroidException:
        msg = "Could not complete http check for app {0} due to unknown error: {1}".format(app.id, traceback.format_exc())
        return (None, msg)


# Check for a new version by looking at the tags in the source repo.
# Whether this can be used reliably or not depends on
# the development procedures used by the project's developers. Use it with
# caution, because it's inappropriate for many projects.
# Returns (None, "a message") if this didn't work, or (version, vercode, tag) for
# the details of the current version.
def check_tags(app, pattern):

    try:

        if app.RepoType == 'srclib':
            build_dir = os.path.join('build', 'srclib', app.Repo)
            repotype = common.getsrclibvcs(app.Repo)
        else:
            build_dir = os.path.join('build', app.id)
            repotype = app.RepoType

        if repotype not in ('git', 'git-svn', 'hg', 'bzr'):
            return (None, 'Tags update mode only works for git, hg, bzr and git-svn repositories currently', None)

        if repotype == 'git-svn' and ';' not in app.Repo:
            return (None, 'Tags update mode used in git-svn, but the repo was not set up with tags', None)

        # Set up vcs interface and make sure we have the latest code...
        vcs = common.getvcs(app.RepoType, app.Repo, build_dir)

        vcs.gotorevision(None)

        last_build = metadata.Build()
        if len(app.builds) > 0:
            last_build = app.builds[-1]

        if last_build.submodules:
            vcs.initsubmodules()

        hpak = None
        htag = None
        hver = None
        hcode = "0"

        tags = vcs.gettags()
        if not tags:
            return (None, "No tags found", None)

        logging.debug("All tags: " + ','.join(tags))
        if pattern:
            pat = re.compile(pattern)
            tags = [tag for tag in tags if pat.match(tag)]
            if not tags:
                return (None, "No matching tags found", None)
            logging.debug("Matching tags: " + ','.join(tags))

        if len(tags) > 5 and repotype in ('git',):
            tags = vcs.latesttags(tags, 5)
            logging.debug("Latest tags: " + ','.join(tags))

        for tag in tags:
            logging.debug("Check tag: '{0}'".format(tag))
            vcs.gotorevision(tag)

            for subdir in possible_subdirs(app):
                if subdir == '.':
                    root_dir = build_dir
                else:
                    root_dir = os.path.join(build_dir, subdir)
                paths = common.manifest_paths(root_dir, last_build.gradle)
                version, vercode, package = common.parse_androidmanifests(paths, app)
                if vercode:
                    logging.debug("Manifest exists in subdir '{0}'. Found version {1} ({2})"
                                  .format(subdir, version, vercode))
                    if int(vercode) > int(hcode):
                        hpak = package
                        htag = tag
                        hcode = str(int(vercode))
                        hver = version

        if not hpak:
            return (None, "Couldn't find package ID", None)
        if hver:
            return (hver, hcode, htag)
        return (None, "Couldn't find any version information", None)

    except VCSException as vcse:
        msg = "VCS error while scanning app {0}: {1}".format(app.id, vcse)
        return (None, msg, None)
    except Exception:
        msg = "Could not scan app {0} due to unknown error: {1}".format(app.id, traceback.format_exc())
        return (None, msg, None)


# Check for a new version by looking at the AndroidManifest.xml at the HEAD
# of the source repo. Whether this can be used reliably or not depends on
# the development procedures used by the project's developers. Use it with
# caution, because it's inappropriate for many projects.
# Returns (None, "a message") if this didn't work, or (version, vercode) for
# the details of the current version.
def check_repomanifest(app, branch=None):

    try:

        if app.RepoType == 'srclib':
            build_dir = os.path.join('build', 'srclib', app.Repo)
            repotype = common.getsrclibvcs(app.Repo)
        else:
            build_dir = os.path.join('build', app.id)
            repotype = app.RepoType

        # Set up vcs interface and make sure we have the latest code...
        vcs = common.getvcs(app.RepoType, app.Repo, build_dir)

        if repotype == 'git':
            if branch:
                branch = 'origin/' + branch
            vcs.gotorevision(branch)
        elif repotype == 'git-svn':
            vcs.gotorevision(branch)
        elif repotype == 'hg':
            vcs.gotorevision(branch)
        elif repotype == 'bzr':
            vcs.gotorevision(None)

        last_build = metadata.Build()
        if len(app.builds) > 0:
            last_build = app.builds[-1]

        if last_build.submodules:
            vcs.initsubmodules()

        hpak = None
        hver = None
        hcode = "0"
        for subdir in possible_subdirs(app):
            if subdir == '.':
                root_dir = build_dir
            else:
                root_dir = os.path.join(build_dir, subdir)
            paths = common.manifest_paths(root_dir, last_build.gradle)
            version, vercode, package = common.parse_androidmanifests(paths, app)
            if vercode:
                logging.debug("Manifest exists in subdir '{0}'. Found version {1} ({2})"
                              .format(subdir, version, vercode))
                if int(vercode) > int(hcode):
                    hpak = package
                    hcode = str(int(vercode))
                    hver = version

        if not hpak:
            return (None, "Couldn't find package ID")
        if hver:
            return (hver, hcode)
        return (None, "Couldn't find any version information")

    except VCSException as vcse:
        msg = "VCS error while scanning app {0}: {1}".format(app.id, vcse)
        return (None, msg)
    except Exception:
        msg = "Could not scan app {0} due to unknown error: {1}".format(app.id, traceback.format_exc())
        return (None, msg)


def check_repotrunk(app, branch=None):

    try:
        if app.RepoType == 'srclib':
            build_dir = os.path.join('build', 'srclib', app.Repo)
            repotype = common.getsrclibvcs(app.Repo)
        else:
            build_dir = os.path.join('build', app.id)
            repotype = app.RepoType

        if repotype not in ('git-svn', ):
            return (None, 'RepoTrunk update mode only makes sense in git-svn repositories')

        # Set up vcs interface and make sure we have the latest code...
        vcs = common.getvcs(app.RepoType, app.Repo, build_dir)

        vcs.gotorevision(None)

        ref = vcs.getref()
        return (ref, ref)
    except VCSException as vcse:
        msg = "VCS error while scanning app {0}: {1}".format(app.id, vcse)
        return (None, msg)
    except Exception:
        msg = "Could not scan app {0} due to unknown error: {1}".format(app.id, traceback.format_exc())
        return (None, msg)


# Check for a new version by looking at the Google Play Store.
# Returns (None, "a message") if this didn't work, or (version, None) for
# the details of the current version.
def check_gplay(app):
    time.sleep(15)
    url = 'https://play.google.com/store/apps/details?id=' + app.id
    headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux i686; rv:18.0) Gecko/20100101 Firefox/18.0'}
    req = urllib2.Request(url, None, headers)
    try:
        resp = urllib2.urlopen(req, None, 20)
        page = resp.read()
    except urllib2.HTTPError as e:
        return (None, str(e.code))
    except Exception as e:
        return (None, 'Failed:' + str(e))

    version = None

    m = re.search('itemprop="softwareVersion">[ ]*([^<]+)[ ]*</div>', page)
    if m:
        html_parser = HTMLParser.HTMLParser()
        version = html_parser.unescape(m.group(1))

    if version == 'Varies with device':
        return (None, 'Device-variable version, cannot use this method')

    if not version:
        return (None, "Couldn't find version")
    return (version.strip(), None)


# Return all directories under startdir that contain any of the manifest
# files, and thus are probably an Android project.
def dirs_with_manifest(startdir):
    for r, d, f in os.walk(startdir):
        if any(m in f for m in [
                'AndroidManifest.xml', 'pom.xml', 'build.gradle']):
            yield r


# Tries to find a new subdir starting from the root build_dir. Returns said
# subdir relative to the build dir if found, None otherwise.
def possible_subdirs(app):

    if app.RepoType == 'srclib':
        build_dir = os.path.join('build', 'srclib', app.Repo)
    else:
        build_dir = os.path.join('build', app.id)

    last_build = metadata.Build()
    if len(app.builds) > 0:
        last_build = app.builds[-1]

    for d in dirs_with_manifest(build_dir):
        m_paths = common.manifest_paths(d, last_build.gradle)
        package = common.parse_androidmanifests(m_paths, app)[2]
        if package is not None:
            subdir = os.path.relpath(d, build_dir)
            logging.debug("Adding possible subdir %s" % subdir)
            yield subdir


def fetch_autoname(app, tag):

    if not app.RepoType or app.UpdateCheckMode in ('None', 'Static'):
        return None

    if app.RepoType == 'srclib':
        build_dir = os.path.join('build', 'srclib', app.Repo)
    else:
        build_dir = os.path.join('build', app.id)

    try:
        vcs = common.getvcs(app.RepoType, app.Repo, build_dir)
        vcs.gotorevision(tag)
    except VCSException:
        return None

    last_build = metadata.Build()
    if len(app.builds) > 0:
        last_build = app.builds[-1]

    logging.debug("...fetch auto name from " + build_dir)
    new_name = None
    for subdir in possible_subdirs(app):
        if subdir == '.':
            root_dir = build_dir
        else:
            root_dir = os.path.join(build_dir, subdir)
        new_name = common.fetch_real_name(root_dir, last_build.gradle)
        if new_name is not None:
            break
    commitmsg = None
    if new_name:
        logging.debug("...got autoname '" + new_name + "'")
        if new_name != app.AutoName:
            app.AutoName = new_name
            if not commitmsg:
                commitmsg = "Set autoname of {0}".format(common.getappname(app))
    else:
        logging.debug("...couldn't get autoname")

    return commitmsg


def checkupdates_app(app, first=True):

    # If a change is made, commitmsg should be set to a description of it.
    # Only if this is set will changes be written back to the metadata.
    commitmsg = None

    tag = None
    msg = None
    vercode = None
    noverok = False
    mode = app.UpdateCheckMode
    if mode.startswith('Tags'):
        pattern = mode[5:] if len(mode) > 4 else None
        (version, vercode, tag) = check_tags(app, pattern)
        if version == 'Unknown':
            version = tag
        msg = vercode
    elif mode == 'RepoManifest':
        (version, vercode) = check_repomanifest(app)
        msg = vercode
    elif mode.startswith('RepoManifest/'):
        tag = mode[13:]
        (version, vercode) = check_repomanifest(app, tag)
        msg = vercode
    elif mode == 'RepoTrunk':
        (version, vercode) = check_repotrunk(app)
        msg = vercode
    elif mode == 'HTTP':
        (version, vercode) = check_http(app)
        msg = vercode
    elif mode in ('None', 'Static'):
        version = None
        msg = 'Checking disabled'
        noverok = True
    else:
        version = None
        msg = 'Invalid update check method'

    if version and vercode and app.VercodeOperation:
        oldvercode = str(int(vercode))
        op = app.VercodeOperation.replace("%c", oldvercode)
        vercode = str(eval(op))
        logging.debug("Applied vercode operation: %s -> %s" % (oldvercode, vercode))

    if version and any(version.startswith(s) for s in [
            '${',  # Gradle variable names
            '@string/',  # Strings we could not resolve
            ]):
        version = "Unknown"

    updating = False
    if version is None:
        logmsg = "...{0} : {1}".format(app.id, msg)
        if noverok:
            logging.info(logmsg)
        else:
            logging.warn(logmsg)
    elif vercode == app.CurrentVersionCode:
        logging.info("...up to date")
    else:
        app.CurrentVersion = version
        app.CurrentVersionCode = str(int(vercode))
        updating = True

    commitmsg = fetch_autoname(app, tag)

    if updating:
        name = common.getappname(app)
        ver = common.getcvname(app)
        logging.info('...updating to version %s' % ver)
        commitmsg = 'Update CV of %s to %s' % (name, ver)

    if options.auto:
        mode = app.AutoUpdateMode
        if mode in ('None', 'Static'):
            pass
        elif mode.startswith('Version '):
            pattern = mode[8:]
            if pattern.startswith('+'):
                try:
                    suffix, pattern = pattern.split(' ', 1)
                except ValueError:
                    raise MetaDataException("Invalid AUM: " + mode)
            else:
                suffix = ''
            gotcur = False
            latest = None
            for build in app.builds:
                if int(build.vercode) >= int(app.CurrentVersionCode):
                    gotcur = True
                if not latest or int(build.vercode) > int(latest.vercode):
                    latest = build

            if int(latest.vercode) > int(app.CurrentVersionCode):
                logging.info("Refusing to auto update, since the latest build is newer")

            if not gotcur:
                newbuild = copy.deepcopy(latest)
                newbuild.disable = False
                newbuild.vercode = app.CurrentVersionCode
                newbuild.version = app.CurrentVersion + suffix
                logging.info("...auto-generating build for " + newbuild.version)
                commit = pattern.replace('%v', newbuild.version)
                commit = commit.replace('%c', newbuild.vercode)
                newbuild.commit = commit
                app.builds.append(newbuild)
                name = common.getappname(app)
                ver = common.getcvname(app)
                commitmsg = "Update %s to %s" % (name, ver)
        else:
            logging.warn('Invalid auto update mode "' + mode + '" on ' + app.id)

    if commitmsg:
        metadatapath = os.path.join('metadata', app.id + '.txt')
        with open(metadatapath, 'w') as f:
            metadata.write_metadata('txt', f, app)
        if options.commit:
            logging.info("Commiting update for " + metadatapath)
            gitcmd = ["git", "commit", "-m", commitmsg]
            if 'auto_author' in config:
                gitcmd.extend(['--author', config['auto_author']])
            gitcmd.extend(["--", metadatapath])
            if subprocess.call(gitcmd) != 0:
                logging.error("Git commit failed")
                sys.exit(1)


config = None
options = None


def main():

    global config, options

    # Parse command line...
    parser = ArgumentParser(usage="%(prog)s [options] [APPID [APPID ...]]")
    common.setup_global_opts(parser)
    parser.add_argument("appid", nargs='*', help="app-id to check for updates")
    parser.add_argument("--auto", action="store_true", default=False,
                        help="Process auto-updates")
    parser.add_argument("--autoonly", action="store_true", default=False,
                        help="Only process apps with auto-updates")
    parser.add_argument("--commit", action="store_true", default=False,
                        help="Commit changes")
    parser.add_argument("--gplay", action="store_true", default=False,
                        help="Only print differences with the Play Store")
    options = parser.parse_args()

    config = common.read_config(options)

    # Get all apps...
    allapps = metadata.read_metadata()

    apps = common.read_app_args(options.appid, allapps, False)

    if options.gplay:
        for app in apps:
            version, reason = check_gplay(app)
            if version is None:
                if reason == '404':
                    logging.info("{0} is not in the Play Store".format(common.getappname(app)))
                else:
                    logging.info("{0} encountered a problem: {1}".format(common.getappname(app), reason))
            if version is not None:
                stored = app.CurrentVersion
                if not stored:
                    logging.info("{0} has no Current Version but has version {1} on the Play Store"
                                 .format(common.getappname(app), version))
                elif LooseVersion(stored) < LooseVersion(version):
                    logging.info("{0} has version {1} on the Play Store, which is bigger than {2}"
                                 .format(common.getappname(app), version, stored))
                else:
                    if stored != version:
                        logging.info("{0} has version {1} on the Play Store, which differs from {2}"
                                     .format(common.getappname(app), version, stored))
                    else:
                        logging.info("{0} has the same version {1} on the Play Store"
                                     .format(common.getappname(app), version))
        return

    for appid, app in apps.iteritems():

        if options.autoonly and app.AutoUpdateMode in ('None', 'Static'):
            logging.debug("Nothing to do for {0}...".format(appid))
            continue

        logging.info("Processing " + appid + '...')

        checkupdates_app(app)

    logging.info("Finished.")

if __name__ == "__main__":
    main()
