#!/usr/bin/env python

__author__ = "Sylvain Pasche <sylvain.pasche@gmail.com>"

import datetime
import glob
import grp
import hashlib
import logging
import math
import multiprocessing
import optparse
import os
from os.path import join
import pwd
import re
import shelve
import shutil
import stat
import subprocess
import sys

thisdir = os.path.abspath(os.path.dirname(__file__))
sys.path.append(join(thisdir, "third_party"))

import srtm
import tempita

log = logging.getLogger(__name__)


# Utilities

SRID_LATLON = 4326

def convert_coordinates(srid_from, srid_to, coordinates):
    if srid_from == srid_to:
        return coordinates
    SRID_TO_FILE = {
        900913: "esri.extra"
    }
    init_from = "+init={0}:{1}".format(
        SRID_TO_FILE.get(srid_from, "epsg"), srid_from)
    init_to = "+init={0}:{1}".format(
        SRID_TO_FILE.get(srid_to, "epsg"), srid_to)
    p = subprocess.Popen(
        ["cs2cs", init_from, "+to", init_to],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE)
    output = p.communicate(input="{0} {1}".format(*coordinates))[0]
    x, y, z = output.split()
    return float(x), float(y)

def convert_bbox(srid_from, srid_to, bbox):
    minx, miny, maxx, maxy = bbox
    return (convert_coordinates(srid_from, srid_to, (minx, miny)) +
        convert_coordinates(srid_from, srid_to, (maxx, maxy)))

def make_dirs_as_project_owner(project_dir, path):
    if os.path.isdir(path):
        # TODO: it should change owner too if needed.
        return
    restore_uid = False
    if os.getuid() == 0:
        os.seteuid(os.stat(project_dir).st_uid)
        restore_uid = True
    try:
        os.makedirs(path)
    finally:
        if restore_uid:
            os.seteuid(0)

def call(cmd, *args, **kwargs):
    """subprocess.check_call wrapper to log the command to be run"""
    log.debug("Running command: %r", cmd)
    subprocess.check_call(cmd, *args, **kwargs)

def apply_patches(patches_dir, target_dir):
    patches = sorted(p for p in os.listdir(patches_dir) if
        p.endswith("diff"))
    for p in patches:
        log.debug("Applying patch %s", p)
        call(["patch", "-p1", "-i", join(patches_dir, p)], cwd=target_dir)

def maybe_unlink(path):
    """Delete the given path and don't complain if it doesn't exist"""
    shutil.rmtree(path, True)
    try:
        os.unlink(path)
    except OSError:
        pass



class Fetcher(object):
    def __init__(self, executor):
        self.executor = executor
        self.cache_dir = join(executor.project_dir, "data", "download_cache")
        if not os.path.isdir(self.cache_dir):
            make_dirs_as_project_owner(executor.project_dir, self.cache_dir)
        shelve_file = join(self.cache_dir, "meta.shelve")
        self.metadata_shelve = shelve.open(shelve_file)
        if os.getuid() == 0:
            os.chown(
                join(self.cache_dir, "meta.shelve"),
                os.stat(executor.project_dir).st_uid,
                os.stat(executor.project_dir).st_gid)

        self.temp_dir = join(self.cache_dir, "temp")

    def _download(self, url, target_path, md5):
        filename = os.path.basename(target_path)

        if (md5 and
            self.metadata_shelve.get("downloaded_" + url) and
            self.metadata_shelve.get("md5_" + url) != md5):
            log.info("md5 changed, removing file")
            del metadata_shelve["md5_" + url]
            del metadata_shelve["extracted_" + url]
            os.unlink(target_path)

        if os.path.isfile(target_path):
            return

        try:
            shutil.rmtree(self.temp_dir, True)
            os.makedirs(self.temp_dir)
            call(["wget", url], cwd=self.temp_dir)

            downloaded_file = join(self.temp_dir, filename)
            actual_md5 = None
            if md5:
                m = hashlib.md5()
                m.update(open(downloaded_file, "rb").read())
                actual_md5 = m.hexdigest()
                if actual_md5 != md5:
                    raise Exception(
                        "Downloaded file {0} doesn't match md5sum "
                        "(expected: {1} actual: {2})".
                        format(url, md5, actual_md5))

            os.rename(downloaded_file, target_path)
            self.metadata_shelve["downloaded_" + url] = True
            if actual_md5:
                self.metadata_shelve["md5_" + url] = actual_md5
        finally:
            shutil.rmtree(self.temp_dir, True)

    def _extract(self, url, target_path, extract_dir):
        if "extracted_" + url in self.metadata_shelve:
            log.debug("File marked as already extracted")
            return
        log.info("Extracting %s to %s", target_path, extract_dir)
        if target_path.endswith(".tgz") or target_path.endswith(".tar.gz"):
            tool = "tar"
            tar_opt = "z"
        elif target_path.endswith(".bz2"):
            tool = "tar"
            tar_opt = "j"
        elif target_path.endswith(".zip"):
            tool = "zip"
        else:
            raise Exception(
                "Doesn't know how to extract {0} archive".format(target_path))

        absolute_extract_dir = join(self.executor.project_dir, extract_dir)
        if not os.path.isdir(absolute_extract_dir):
            os.makedirs(absolute_extract_dir)

        if tool == "tar":
            call(["tar", tar_opt + "xf", target_path], cwd=absolute_extract_dir)
        elif tool == "zip":
            call(["unzip", "-q", "-d", absolute_extract_dir, target_path])
        else:
            assert False
        self.metadata_shelve["extracted_" + url] = True
        self.metadata_shelve.sync()

    def fetch(self, resource):
        log.debug("Fetching: %s", resource)
        resource = list(resource) + [None, None]

        url = resource.pop(0)
        extract_dir = resource.pop(0)
        md5 = resource.pop(0)

        filename = url.split("/")[-1]
        target_path = join(self.cache_dir, filename)

        was_downloaded = self._download(url, target_path, md5)
        if extract_dir:
            self._extract(url, target_path, extract_dir)

    def clean(self, resource):
        r = list(resource) + [None, None]
        url = r.pop(0)
        extract_dir = r.pop(0)
        # TODO: 
        assert not extract_dir, "Cleaning extracted resource not implemented yet"
        maybe_unlink(self.get_downloaded_path(resource))

    def get_downloaded_path(self, resource):
        url = resource[0]
        filename = url.split("/")[-1]
        return join(self.cache_dir, filename)


class SVNCheckoutMixin(object):
    def init_svn(self, checkout_dir, url, revision, export=False):
        self.svn_checkout_dir = checkout_dir
        self.svn_url = url
        self.svn_revision = revision
        self.svn_export = export

    def get_source_dir(self, url):
        return join(self.executor.build_dir, url.rstrip("/").split("/")[-1])

    def _is_checkedout(self):
        if not os.path.isdir(self.svn_checkout_dir):
            return False

        # Don't erase an exported checkout.
        if self.svn_export:
            return True

        actual_revision = subprocess.Popen(
            "svnversion", cwd=self.svn_checkout_dir, stdout=subprocess.PIPE
            ).communicate()[0].strip()

        if actual_revision != self.svn_revision:
            log.info("Checked out revision doesn't match. Deleting checkout.")
            shutil.rmtree(self.svn_checkout_dir)
            return False

        return True

    def fetch_svn(self):
        if self._is_checkedout():
            return

        svn_command = "export" if self.svn_export else "co"
        cmd = ["svn", svn_command, "-r", self.svn_revision,
            self.svn_url, self.svn_checkout_dir]
        log.info("Checking out project with command: %s", cmd)
        call(cmd, stdout=subprocess.PIPE)


class Bundle(object):
    def __init__(self, executor):
        self.executor = executor
        # shortcuts
        self.config = self.executor.config
        self.options = self.executor.options
        self.project_dir = self.executor.project_dir

        self.name = self.__class__.__name__.lower()

    def __repr__(self):
        return "<Bundle:%s>" % (self.name)

    @property
    def dependencies(self):
        return []

    # To override
    def create_project(self):
        pass

    def install_packages(self, packages):
        if isinstance(packages, str):
            packages = packages.split()

        to_install = [p for p in packages if
            subprocess.call("dpkg -s {0} | grep -q 'Status:.*\sinstalled'".format(p),
                shell=True, stdout=subprocess.PIPE)]
        if not to_install:
            return
        log.info("Installing packages: %s", to_install)
        call(["aptitude", "-y", "install"] + packages)

    # To override
    def system_setup(self):
        pass

    def fetch_resources(self, resources):
        for resource in resources:
            self.executor.fetcher.fetch(resource)

    def clean_resources(self, resources):
        for resource in resources:
            self.executor.fetcher.clean(resource)

    # To override
    def download(self):
        pass

    # To override
    def build(self):
        pass


class CoreBundle(Bundle):
    def _copy_template_directory(self):
        target_path = self.project_dir
        for path, dirlist, filelist in os.walk(join(self.executor.oss_dir, "template")):
            for name in filelist:
                base_parts_count = len(self.executor.oss_dir.split(os.path.sep))
                relative_path = os.sep.join(path.split(os.sep)[base_parts_count + 1:])

                source = join(path, name)
                target = join(target_path, relative_path, name)

                if not self.options.overwrite and os.path.isfile(target):
                    raise Exception("Target %r already exists, not replacing", target)
                if not os.path.isdir(os.path.dirname(target)):
                    os.makedirs(os.path.dirname(target))
                shutil.copy(source, target)

    def create_project(self):
        self._copy_template_directory()

    def system_setup(self):
        self.install_packages("unzip")

    def _process_dot_in_file(self, content, vars, template_dir):
        if content.startswith("# Tempita"):
            content = content[content.index("\n") + 1:]
            def get_template(name, from_template):
                path = join(template_dir, name)
                return from_template.__class__.from_filename(
                    path, namespace=from_template.namespace,
                    get_template=from_template.get_template)

            tmpl = tempita.Template(content, get_template=get_template)
            return tmpl.substitute(vars)

        for (key, value) in vars.iteritems():
            content = content.replace("@@" + key + "@@", str(value))
        return content

    def _copy_dot_in_files(self):

        c = self.config
        REPLACED_VARIABLES = {
            "DOCUMENT_ROOT": join(self.project_dir, "htdocs"),
            "PROJECT_DIR": self.project_dir,
            "APACHE_SERVER_ALIASES": " ".join(c.APACHE_SERVER_ALIASES),
            "USE_TILECACHE_COMMENT": "" if c.USE_TILECACHE else "#",

            # Convert into JS friendly values.
            "EXTENT": list(c.EXTENT),
            "USE_MAPSERVER": 1 if c.USE_MAPSERVER else 0,
            "USE_TILECACHE": 1 if c.USE_TILECACHE else 0,
            # TODO: add link to public repo when available.
            "BUILD_INFO": "Built with osm-server-setup on {0}".format(datetime.datetime.now()),
        }

        target_path = self.project_dir
        for path, dirlist, filelist in os.walk(self.project_dir):
            for exclude in ["data", "osm-server-setup", ".git", ".hg", ".svn", "build"]:
                if exclude in dirlist:
                    dirlist.remove(exclude)
            for name in filelist:
                if not name.endswith(".in"):
                    continue
                source = join(path, name)
                target = source[:-len(".in")]

                log.debug("Generating file: %r -> %r", source, target)

                with open(target, 'wb') as f:
                    content = open(source).read()
                    vars = self.config.__dict__.copy()
                    vars.update(REPLACED_VARIABLES)
                    vars["GENERATED_WARNING"] = ("Warning, this file is generated"
                        " from %s. Edit that file instead and run the update "
                        "script." % source)

                    content = self._process_dot_in_file(content, vars, path)
                    f.write(content)
                try:
                    shutil.copymode(source, target)
                except OSError, e:
                    # This might happen if the user is not the owner.
                    log.warn("Error while updating mode: %s", e)

    def build(self):
        self._copy_dot_in_files()


class SetupDatabase(Bundle):

    def _get_psql_env(self):
        env = os.environ.copy()
        env["PGHOST"] = self.config.DB_HOST
        env["PGPORT"] = self.config.DB_PORT
        env["PGDATABASE"] = self.config.DB_NAME
        env["PGUSER"] = self.config.DB_USER
        env["PGPASSWORD"] = self.config.DB_PASSWORD
        return env

    def query_succeeds(self, sql):
        try:
            call(["psql", "-q", "-o/dev/null", "-c", sql],
                env=self._get_psql_env())
        except subprocess.CalledProcessError:
            return False

        return True

    def execute_sql(self, sql):
        call(["psql", "-c", sql], env=self._get_psql_env())

    def execute_sql_file(self, file):
        if isinstance(file, str):
            file = open(file)
        p = subprocess.Popen(
            "psql", env=self._get_psql_env(), stdin=file,
            stdout=subprocess.PIPE)
        p.communicate()
        if p.returncode:
            raise subprocess.CalledProcessError(p.returncode, "psql")

    def _call(self, cmd):
        cmd = cmd.format(**self.config.__dict__)
        log.debug("Running commmand: %r", cmd)
        p = subprocess.Popen(
            cmd % self.config.__dict__, shell=True,
            stderr=subprocess.STDOUT,
            stdout=None if self.options.verbose else subprocess.PIPE)
        p.communicate()
        if p.returncode:
            raise subprocess.CalledProcessError(p.returncode, "psql")

    def system_setup(self):
        """
        In case of error:
            ERROR:  new encoding (UTF8) is incompatible with the encoding of the template database (SQL_ASCII)

        (see http://jacobian.org/writing/pg-encoding-ubuntu/)

        pg_dropcluster --stop 8.4 main
        pg_createcluster --start -e UTF-8 8.4 main
        """
        self.install_packages("postgresql-8.4-postgis postgresql-8.4 postgresql-contrib-8.4")

        if self.query_succeeds("select * from geometry_columns"):
            return

        os.seteuid(pwd.getpwnam("postgres").pw_uid)

        try:
            self._call("createuser -SDR {DB_USER}")
            self._call("""psql -c "ALTER USER {DB_USER} WITH PASSWORD '{DB_PASSWORD}'" """)
        except subprocess.CalledProcessError, e:
            log.warn("Failed to create user. It already exists? (%s)", e)

        self._call("createdb -E UTF8 -O {DB_USER} {DB_NAME}")
        self._call("createlang plpgsql {DB_NAME}")
        # TODO: config for .sql paths
        self._call("psql -d {DB_NAME} -f /usr/share/postgresql/8.4/contrib/_int.sql")
        self._call("psql -d {DB_NAME} -f /usr/share/postgresql/8.4/contrib/postgis-1.5/postgis.sql")
        self._call("psql -d {DB_NAME} -f /usr/share/postgresql/8.4/contrib/postgis-1.5/spatial_ref_sys.sql")
        self._call("psql {DB_NAME} -c 'ALTER TABLE geometry_columns OWNER TO {DB_USER}'")
        self._call("psql {DB_NAME} -c 'ALTER TABLE spatial_ref_sys OWNER TO {DB_USER}'")
        self._call("psql {DB_NAME} -c 'ALTER TABLE geography_columns OWNER TO {DB_USER}'")

        os.seteuid(0)

    def system_setup_clean(self):
        input = raw_input(
            "This will delete the database {DB_NAME} and user {DB_USER}. "
            "Are you sure [y/n]? ".format(**self.config.__dict__))
        if input != "y":
            return
        os.seteuid(pwd.getpwnam("postgres").pw_uid)

        try:
            self._call("dropdb {DB_NAME}")
        except subprocess.CalledProcessError:
            pass
        self._call("dropuser {DB_USER}")

        os.seteuid(0)


class Osm2pgsqlBuild(Bundle, SVNCheckoutMixin):
    """
    See http://wiki.openstreetmap.org/wiki/Osm2pgsql
    """

    def __init__(self, *args, **kwargs):
        super(Osm2pgsqlBuild, self).__init__(*args, **kwargs)
        checkout_dir = join(
            self.executor.build_dir,
            self.config.OSM2PGSQL_SVN_URL.rstrip("/").split("/")[-1])

        self.init_svn(
            checkout_dir,
            self.config.OSM2PGSQL_SVN_URL,
            self.config.OSM2PGSQL_SVN_REVISION)

    def system_setup(self):
        self.install_packages(
            "subversion build-essential libxml2-dev libgeos-dev libpq-dev "
            "libbz2-dev proj autoconf libtool")

    def download(self):
        self.fetch_svn()

    def build(self):
        if os.path.isfile(join(self.svn_checkout_dir, "osm2pgsql")):
            log.debug("Executable osm2pgsql already built")
            return

        call(join(self.svn_checkout_dir, "autogen.sh"),
            cwd=self.svn_checkout_dir)
        call(join(self.svn_checkout_dir, "configure"),
            cwd=self.svn_checkout_dir)
        call("sed -i 's/-g -O2/-O2 -march=native -fomit-frame-pointer/' Makefile",
            cwd=self.svn_checkout_dir, shell=True)
        call(["make", "-j", str(multiprocessing.cpu_count())],
            cwd=self.svn_checkout_dir)


class Osmosis(Bundle):
    """
    See
    http://wiki.openstreetmap.org/wiki/Minutely_Mapnik
    http://wiki.openstreetmap.org/wiki/Osmosis
    http://wiki.openstreetmap.org/wiki/Osmosis/Detailed_Usage
    """
    OSMOSIS_VER = "0.39"

    def __init__(self, *args, **kwargs):
        super(Osmosis, self).__init__(*args, **kwargs)
        self.work_dir = join(self.project_dir, "data", "osmosis")
        self.changes_file = join(self.work_dir, "changes.osm.gz")
        self.osmosis = join(
            self.project_dir, "build", "osmosis-{0}".format(self.OSMOSIS_VER),
            "bin", "osmosis")

    def system_setup(self):
        self.install_packages("openjdk-6-jre")

    def download(self):
        self.fetch_resources([
            ("http://dev.openstreetmap.org/~bretth/osmosis-build/osmosis-latest.tgz",
                "build", "793a1cff312ed003e90ee090d77c33db"),
        ])

    def build(self):
        if not os.path.isdir(self.work_dir):
            os.makedirs(self.work_dir)
        if os.path.isfile(join(self.work_dir, "configuration.txt")):
            return
        call([self.osmosis, "--read-replication-interval-init",
            "workingDirectory=" + self.work_dir])

    def read_replication(self):
        call([self.osmosis, "--read-replication-interval",
            "workingDirectory=" + self.work_dir, "--simplify-change",
            "--write-xml-change", self.changes_file])


class OsmData(Bundle):
    """
    See
    http://wiki.openstreetmap.org/wiki/Osm2pgsql
    http://wiki.openstreetmap.org/wiki/Mapnik
    """

    def __init__(self, executor, tables_prefix):
        super(OsmData, self).__init__(executor)

        self.tables_prefix = tables_prefix
        self.name += "_" + self.tables_prefix
        self.osm_resources = [(url,) for url in self.config.OSM_DATA_URLS]
        self.did_load_data = False

    @property
    def dependencies(self): 
        deps = [SetupDatabase, Osm2pgsqlBuild]
        if self.config.USE_OSMOSIS:
            deps.append(Osmosis)
        return deps

    def download(self):
        self.fetch_resources(self.osm_resources)

    def download_clean(self):
        self.clean_resources(self.osm_resources)

    def _call_osm2pgsql(self, args):
        osm2pgsql_bundle = self.executor.get_bundle("osm2pgsqlbuild")
        style_path = self.config.OSM_DATA_STYLE_PATH.get(
            self.tables_prefix,
            join(osm2pgsql_bundle.svn_checkout_dir, "default.style"))
        cmd = [
            join(osm2pgsql_bundle.svn_checkout_dir, "osm2pgsql"),
            "-H", self.config.DB_HOST,
            "-P", self.config.DB_PORT,
            "-U", self.config.DB_USER,
            "-d", self.config.DB_NAME,
            "-p", self.tables_prefix,
            "--bbox", ",".join(str(c) for c in self.config.EXTENT),
            "-S", style_path,
        ]
        if self.config.OSM2PGSQL_SLIM_MODE:
            cmd.append("--slim")
        cmd.extend(args)

        env = os.environ.copy()
        env["PGPASS"] = self.config.DB_PASSWORD

        log.info("osm2pgsql command: %s", cmd)
        call(cmd, env=env)

    def load_data(self):
        db_bundle = self.executor.get_bundle("setupdatabase")
        # Assumes that if all the osm tables are present, the import doesn't
        # need to run.
        if all([db_bundle.query_succeeds(
            'select * from "{0}_{1}" limit 1'.format(self.tables_prefix, t)) for
            t in ["point", "line", "polygon", "roads"]]):
            return

        args = []
        for r in self.osm_resources:
            args.append(self.executor.fetcher.get_downloaded_path(r))
        self._call_osm2pgsql(args)

        self.did_load_data = True

    def load_data_clean(self):
        db_bundle = self.executor.get_bundle("setupdatabase")

        for table in ["point", "line", "polygon", "roads"]:
            db_bundle.execute_sql(
                "select DropGeometryTable('{prefix}_{table}')".format(
                    prefix=self.tables_prefix,
                    table=table))

    def load_replication(self):
        osmosis_bundle = self.executor.get_bundle("osmosis")
        self._call_osm2pgsql(["--append", osmosis_bundle.changes_file])


class SRTMData(Bundle):
    """
    See
    http://wiki.openstreetmap.org/wiki/Contours
    http://wiki.openstreetmap.org/wiki/SRTM

    Hill shading:
    http://wiki.openstreetmap.org/wiki/Hillshading_with_Mapnik
    http://wiki.openstreetmap.org/wiki/HikingBikingMaps
    http://www.perrygeo.net/wordpress/?p=7
    """

    CONTOURS_TABLE = "contours"

    # Documentation is confusing about this value. The official
    # documentation says it's -32768 (http://dds.cr.usgs.gov/srtm/version2_1/Documentation/SRTM_Topo.pdf)
    # however the Wiki page on http://wiki.openstreetmap.org/wiki/Contours
    # mentions 32767 (somebody reported -32767).
    # If the wrong value is used, the generated shapefile size explodes
    # (247M vs 15M on some data I tested).
    NO_DATA_VALUE = -32768

    def __init__(self, executor):
        super(SRTMData, self).__init__(executor)
        self.srtm_dir = join(self.project_dir, "data", "srtm")
        make_dirs_as_project_owner(self.project_dir, self.srtm_dir)

        self.downloader = srtm.SRTMDownloader(cachedir=self.srtm_dir)

        self.tiles_coordinates = []
        minx, miny, maxx, maxy = self.config.EXTENT
        for x in range(int(math.floor(minx)), int(math.ceil(maxx))):
            for y in range(int(math.floor(miny)), int(math.ceil(maxy))):
                self.tiles_coordinates.append((x, y))

        self.perrygeo_dir = join(self.project_dir, "build", "perrygeo")
        self.hillshade = join(self.perrygeo_dir, "demtools", "bin", "hillshade")

    @property
    def dependencies(self):
        return [SetupDatabase]

    def system_setup(self):
        self.install_packages("postgis gdal-bin python-gdal")
        if self.config.USE_HILLSHADING:
            self.install_packages("mercurial libgdal1-dev")

    def download(self):
        # Custom hgt tiles.
        for url, filename in self.config.SRTM_HGT_URLS:
            if os.path.isfile(join(self.srtm_dir, filename)):
                continue
            url_filename = url.split("/")[-1]
            maybe_unlink(join(self.srtm_dir, url_filename))
            call(["wget", url], cwd=self.srtm_dir)
            os.rename(
                join(self.srtm_dir, url_filename),
                join(self.srtm_dir, filename))

        # NASA tiles.
        self.downloader.loadFileList()
        for (x, y) in self.tiles_coordinates:
            self.downloader.getTile(y, x)

        if not self.config.USE_HILLSHADING:
            return

        if os.path.isdir(self.perrygeo_dir):
            return

        call("hg clone https://perrygeo.googlecode.com/hg/ perrygeo",
            shell=True, cwd=join(self.project_dir, "build"))

        apply_patches(
            join(self.executor.oss_dir, "patches", "perrygeo"),
            self.perrygeo_dir)
        os.mkdir(join(self.perrygeo_dir, "demtools", "bin"))

    def build(self):
        if os.path.isfile(self.hillshade):
            log.debug("Executable osm2pgsql already built")
            return

        call("make", cwd=join(self.perrygeo_dir, "demtools"))

    def _build_merged_hgt(self):
        if os.path.isfile(join(self.srtm_dir, "contours.hgt")):
            return

        # Clean previously generated files.
        for p in glob.glob(join(self.srtm_dir, "contours*")):
            if "contours_hillshading.tif" in p:
                continue
            os.unlink(p)

        log.info("Uncompressing .hgt files")
        to_merge = []
        self.downloader.loadFileList()
        for (x, y) in self.tiles_coordinates:
            _, filename = self.downloader.filelist[y, x]
            call(["unzip", "-qo", filename], cwd=self.srtm_dir)
            to_merge.append(filename.replace(".zip", ""))

        if self.config.SRTM_RESIZE_DIMENSION > 0:
            # Reduce size of .hgt files.
            log.info("Warping .hgt files")
            for f in to_merge:
                maybe_unlink(join(self.srtm_dir, f + "_tmp"))
                call("gdalwarp -rcs -order 3 -ts {width} {height} -multi "
                    "{input} {output}".format(
                    width=self.config.SRTM_RESIZE_DIMENSION,
                    height=self.config.SRTM_RESIZE_DIMENSION,
                    input=f, output=f + "_tmp"),
                    shell=True, cwd=self.srtm_dir)
                maybe_unlink(join(self.srtm_dir, f))
                os.rename(
                    join(self.srtm_dir, f + "_tmp"),
                    join(self.srtm_dir, f))

        log.info("Merging .hgt files together")
        call(["gdal_merge.py", "-o", "contours.hgt"] + to_merge,
            cwd=self.srtm_dir)
        for p in glob.glob(join(self.srtm_dir, "[NS]*hgt")):
            os.unlink(p)

    def _populate_database_table(self):
        db_bundle = self.executor.get_bundle("setupdatabase")
        # Assumes that if all the contours table is present, the import doesn't
        # need to run.
        if db_bundle.query_succeeds(
            'select * from "{0}" limit 1'.format(self.CONTOURS_TABLE)):
            log.info("Contours table already there, bailing out.")
            return

        CONTOURS_INTERVAL = 10

        log.info("Generating contours shapefile")
        for ext in ("shp", "shx", "dbf", "prj"):
            maybe_unlink(join(self.srtm_dir, "contours." + ext))
        call("gdal_contour -i {0} -snodata {1} -a height contours.hgt "
            "contours.shp".format(CONTOURS_INTERVAL, self.NO_DATA_VALUE),
            shell=True, cwd=self.srtm_dir)

        log.info("Reprojecting shapfile")
        call("ogr2ogr -t_srs EPSG:{0} contours_reprojected.shp contours.shp".
            format(self.config.SRID_OSM),
            shell=True, cwd=self.srtm_dir)

        GEOCOLUMN = "way"

        log.info("Converting shape and loading sql")
        shp2pgsqlPopen = subprocess.Popen(
            "shp2pgsql -s {srid} -g {geocolumn} -I "
            "contours_reprojected.shp {table}".format(
                srid=self.config.SRID_OSM,
                geocolumn=GEOCOLUMN,
                table=self.CONTOURS_TABLE),
            shell=True,
            cwd=self.srtm_dir,
            stdout=subprocess.PIPE)

        db_bundle.execute_sql_file(shp2pgsqlPopen.stdout)

    def _create_hillshading(self):
        if os.path.isfile(join(self.srtm_dir, "contours_hillshading.tif")):
            return

        # TODO: create options for some of the parameters

        maybe_unlink(join(self.srtm_dir, "contours.tif"))
        call('gdal_translate -co "TILED=YES" contours.hgt contours.tif',
            shell=True, cwd=self.srtm_dir)

        maybe_unlink(join(self.srtm_dir, "contours_warped.tif"))
        call('gdalwarp -co "TILED=YES" -srcnodata {0} '
            '-t_srs "+init=esri.extra:900913" '
            '-rcs -order 3 -tr 30 30 -multi contours.tif contours_warped.tif'.
            format(self.NO_DATA_VALUE),
            shell=True, cwd=self.srtm_dir)

        call(self.hillshade + " contours_warped.tif contours_hillshading.tif -z2",
            shell=True, cwd=self.srtm_dir)

    def load_data(self):
        self._build_merged_hgt()

        self._populate_database_table()

        if self.config.USE_HILLSHADING:
            self._create_hillshading()

    def load_data_clean(self):
        maybe_unlink(join(self.srtm_dir, "contours.hgt"))
        maybe_unlink(join(self.srtm_dir, "contours_hillshading.tif"))

        db_bundle = self.executor.get_bundle("setupdatabase")
        db_bundle.execute_sql(
            "select DropGeometryTable('{0}')".format(self.CONTOURS_TABLE))


class MapnikConfig(Bundle, SVNCheckoutMixin):
    """
    See http://wiki.openstreetmap.org/wiki/Mapnik
    """

    def __init__(self, executor, instance_name):
        super(MapnikConfig, self).__init__(executor)

        self.name += "_" + instance_name
        self.instance_name = instance_name
        self.mapnik_dir = join(self.project_dir, "mapnik_" + instance_name)
        self.init_svn(
            self.mapnik_dir,
            self.config.MAPNIK_SVN_URL,
            self.config.MAPNIK_SVN_REVISION,
            export=True)

    @property
    def dependencies(self):
        return [(OsmData, 'osm_mapnik')]

    def create_project(self):
        self.fetch_svn()

    def system_setup(self):
        self.install_packages("python-mapnik")

    def download(self):
        # TODO put in a bundle shared between mapnik and mapserver with common data.
        self.fetch_resources([
            ("http://tile.openstreetmap.org/world_boundaries-spherical.tgz",
                "data", "4feb2f60a37bbe4e8a33596befcd0a1c"),
            ("http://tile.openstreetmap.org/processed_p.tar.bz2",
                "data/world_boundaries"),
            ("http://tile.openstreetmap.org/shoreline_300.tar.bz2",
                "data/world_boundaries"),
            ("http://www.naturalearthdata.com/http//www.naturalearthdata.com/download/10m/cultural/10m-populated-places.zip",
                "data/world_boundaries", "c7dc8df2ab4a325f5c4fde3e1727655e"),
            ("http://www.naturalearthdata.com/http//www.naturalearthdata.com/download/110m/cultural/110m-admin-0-boundary-lines.zip",
                "data/world_boundaries", "1d116cde1491e514f3f49224682f82b5"),
        ])

    def build(self):
        cmd = [
            "python",
            "generate_xml.py",
            "--host", self.config.DB_HOST,
            "--host", self.config.DB_HOST,
            "--port", self.config.DB_PORT,
            "--user", self.config.DB_USER,
            "--dbname", self.config.DB_NAME,
            "--password", self.config.DB_PASSWORD,
            "--world_boundaries", join(self.project_dir, "data", "world_boundaries"),
            "--prefix", "osm_mapnik",
        ]
        log.debug("Running: %s", cmd)
        call(cmd, cwd=self.mapnik_dir)

    def generate(self):
        TILES_DIR = join(
            self.project_dir, "data", "tiles", "mapnik_" + self.instance_name)
        if not os.path.isdir(TILES_DIR):
            os.makedirs(TILES_DIR)

        env = os.environ.copy()
        env["MAPNIK_MAP_FILE"] = "osm.xml"
        env["MAPNIK_TILE_DIR"] = TILES_DIR
        # TODO: update generate_tiles.py script to generate the right area.
        if False:
            call(["python", "generate_tiles.py"], cwd=self.mapnik_dir, env=env)


class MapnikOGCServer(Bundle):
    def download(self):
        ogcserver_dir = join(self.project_dir, "build", "OGCServer")
        if os.path.isdir(ogcserver_dir):
            return

        call("git clone https://github.com/mapnik/OGCServer.git",
            shell=True, cwd=join(self.project_dir, "build"))

        apply_patches(
            join(self.executor.oss_dir, "patches", "mapnik_ogcserver"),
            ogcserver_dir)


class MapserverBuild(Bundle):
    MS_VERSION = "6.0.0"

    def __init__(self, *args, **kwargs):
        super(MapserverBuild, self).__init__(*args, **kwargs)
        self.ms_dir = join(self.project_dir, "build", "mapserver-%s" % self.MS_VERSION)

    def system_setup(self):
        # Build list of dependencies for building mapserver
        apt_output = subprocess.Popen(
            "apt-get -s build-dep mapserver", shell=True, stdout=subprocess.PIPE
            ).communicate()[0]
        lines = [l for l in apt_output.splitlines() if l.startswith("Inst")]
        self.install_packages([line.split()[1] for line in lines])

    def download(self):
        self.fetch_resources([
            ("http://download.osgeo.org/mapserver/mapserver-%s.tar.gz" % self.MS_VERSION,
                "build", "5bcb1a6fb4a743e9f069466fbdf4ab76"),
        ])

    def build(self):
        if os.path.isfile(join(self.ms_dir, "mapserv")):
            log.debug("Executable already built")
            return

        # from http://ftp.de.debian.org/debian/pool/main/m/mapserver/mapserver_5.6.6-1.1.debian.tar.gz
        MS_BUILD_CMD = ("./configure --enable-debug  --without-tiff --without-pdf "
            "--with-gd=/usr --with-freetype=/usr --with-fribidi-config "
            "--with-ming --with-zlib=/usr --with-png=/usr --with-xpm=/usr "
            "--with-jpeg=/usr --with-gdal --with-ogr --with-proj --with-eppl "
            "--with-postgis --with-wcs --with-sos --with-wms --with-wmsclient "
            "--with-wfs --with-wfsclient --with-threads --with-geos "
            "--with-fastcgi --with-agg --with-experimental-png")

        call(MS_BUILD_CMD, shell=True, cwd=self.ms_dir)
        call(["make", "-j", str(multiprocessing.cpu_count())], cwd=self.ms_dir)

        cgi_dir = join(self.project_dir, "apache", "cgi-bin")
        if not os.path.isdir(cgi_dir):
            os.makedirs(cgi_dir)
        shutil.copy(join(self.ms_dir, "mapserv"), cgi_dir)


# TODO: Implement multiple instances, like for Mapnik.
class MapserverConfig(Bundle, SVNCheckoutMixin):
    """
    See http://trac.osgeo.org/mapserver/wiki/RenderingOsmData
    """
    TABLES_PREFIX = "osm_mapserver"

    def __init__(self, *args, **kwargs):
        super(MapserverConfig, self).__init__(*args, **kwargs)

        self.ms_utils_dir = join(self.project_dir, "mapserver-utils")
        self.init_svn(
            self.ms_utils_dir,
            self.config.MAPSERVER_SVN_URL,
            self.config.MAPSERVER_SVN_REVISION,
            export=True)

    @property
    def dependencies(self):
        # TODO: should specifiy osm argument to put in other tables.
        return [(OsmData, self.TABLES_PREFIX), MapserverBuild]

    def _write_settings(self):

        with open(join(self.ms_utils_dir, 'dbconnection'), 'wb') as f:
            f.write(
                '#define _db_connection "host=%(DB_HOST)s dbname=%(DB_NAME)s '
                'user=%(DB_USER)s password=%(DB_PASSWORD)s port=%(DB_PORT)s"\n' %
                self.config.__dict__)

        MAKEFILE_PARAMS = {
            "OSM_PREFIX": "osm_mapserver_",
            "OSM_SRID": str(self.config.SRID_OSM),
        }

        makefile = join(self.ms_utils_dir, 'Makefile')
        lines = open(makefile).readlines()
        for (i, line) in enumerate(lines):
            try:
                key, val = line.split("=")
            except ValueError:
                continue
            if key not in MAKEFILE_PARAMS:
                continue
            lines[i] = "{0}={1}\n".format(key, MAKEFILE_PARAMS[key])

        open(makefile, "wb").writelines(lines)

        # TODO: replace EXTENT from config in osmtemplate.map

    def create_project(self):
        self.fetch_svn()

        # Skip if already applied
        data_makefile = join(self.ms_utils_dir, "data", "Makefile")
        if "unzip -o" not in open(data_makefile).read():
            apply_patches(
                join(self.executor.oss_dir, "patches", "mapserver-utils"),
                self.ms_utils_dir)
        self._write_settings()

    def system_setup(self):
        self.install_packages("cpp make patch")

    def download(self):
        self.fetch_resources([
            ("http://thematicmapping.org/downloads/TM_WORLD_BORDERS-0.3.zip",
                None, "7ac5c67b43e1dc9233cdb48bdf018a6c"),
            ("http://www.naturalearthdata.com/http//www.naturalearthdata.com/"
                "download/10m/cultural/10m-admin-0-boundary-lines-land.zip",
                None, "f3dc23b8d3ede755d56b50f2ca7d0612"),
        ])

        # Try to reuse previously downloaded archives by creating hard links
        # into mapserver-utils/data so that they won't be downloaded again.
        DATA_ARCHIVES = [
            "processed_p.tar.bz2",
            "world_boundaries-spherical.tgz",
            "10m-populated-places.zip",
            "10m-admin-0-boundary-lines-land.zip",
            "110m-admin-0-boundary-lines.zip",
            "shoreline_300.tar.bz2",
            "TM_WORLD_BORDERS-0.3.zip",
        ]
        for archive in DATA_ARCHIVES:
            downloaded_archive = join(
                self.project_dir, "data", "download_cache", archive)
            if not os.path.isfile(downloaded_archive):
                continue
            link_source = join(self.ms_utils_dir, "data", archive)
            if os.path.isfile(link_source):
                continue
            print downloaded_archive, link_source
            os.link(downloaded_archive, link_source)

        try:
            call("cp -rl data/world_boundaries/* mapserver-utils/data/",
                shell=True, cwd=self.project_dir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            pass

        call("touch mapserver-utils/data/*shp", shell=True, cwd=self.project_dir)

        call("make", cwd=join(self.ms_utils_dir, "data"))

    def build(self):
        self._write_settings()
        os.unlink(join(self.ms_utils_dir, "postprocess.sql"))
        call("make", cwd=self.ms_utils_dir)

    def load_data(self):
        osm_load_bundle = self.executor.get_bundle(
            "osmdata_" + self.TABLES_PREFIX)
        if osm_load_bundle.did_load_data:
            db_bundle = self.executor.get_bundle("setupdatabase")
            log.info("Executing postprocess.sql script")
            db_bundle.execute_sql_file(
                join(self.ms_utils_dir, "postprocess.sql"))

        # Try building an image to detect issues.
        ms_bundle = self.executor.get_bundle("mapserverbuild")

        call([join(ms_bundle.ms_dir, "shp2img"), "-m", "osm-mapserver.map",
                "-o", "osm.png", "-e"] +
                [str(int(c)) for c in self.config.EXTENT_OSM],
            cwd=self.ms_utils_dir)


class TileCache(Bundle):
    # Group under which TileCache will be run. The cache directory will be
    # made writable and group sgid to that group, so that both Apache and
    # the user running this script can both write tile images.
    TILECACHE_GROUP = "www-data"
    TILECACHE_VERSION = "2.11"

    def __init__(self, *args, **kwargs):
        super(TileCache, self).__init__(*args, **kwargs)
        self.tc_dir = join(self.project_dir, "build", "tilecache")
        self.cache_dir = join(self.project_dir, "data", "tiles", "tc_cache")

    def system_setup(self):
        self.install_packages("python-imaging")
        if self.config.USE_APACHE:
            self.install_packages("libapache2-mod-wsgi python-paste")

        make_dirs_as_project_owner(self.project_dir, self.cache_dir)

        tc_group_id = grp.getgrnam(self.TILECACHE_GROUP).gr_gid

        os.chown(self.cache_dir, -1, tc_group_id)
        os.chmod(self.cache_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_ISGID)

        log.warn("*IMPORTANT*: you must add the user running this script to "
            "the {0!r} group (reboot or relogin to apply) so that tiles can be "
            "written by both Apache and that user.".format(self.TILECACHE_GROUP))
        if "OSS_NON_INTERACTIVE" not in os.environ:
            raw_input("press enter to continue...")

    def download(self):
        self.fetch_resources([
            ("http://tilecache.org/tilecache-%s.tar.gz" % self.TILECACHE_VERSION,
                "build", "ff0153452a9e88a8d00405fb58d689df"),
        ])
        try:
            old_dir = glob.glob(join(self.project_dir, "build", "tilecache-*"))[0]
        except IndexError:
            return
        if not os.path.isdir(old_dir):
            return
        maybe_unlink(self.tc_dir)
        os.rename(old_dir, self.tc_dir)

    def build(self):
        tc_config = join(self.project_dir, "tilecache", "tilecache.cfg")
        content = open(tc_config).read()
        # TODO: use tempita for instead
        if "@@MAPNIK_START@@" not in content:
            return
        before, template, after = re.search(
            "(.*)@@MAPNIK_START@@\n(.*)@@MAPNIK_END@@\n(.*)", content, re.DOTALL).groups()
        result = before
        for name in self.config.MAPNIK_INSTANCES:
            result += template.replace("@@MAPNIK_NAME@@", name) + "\n"
        result += after
        open(tc_config, "wb").write(result)

    def generate(self):
        seed_script = join(self.tc_dir, "tilecache_seed.py")
        tc_config = join(self.project_dir, "tilecache", "tilecache.cfg")

        layers = []
        for name in self.config.MAPNIK_INSTANCES:
            layers.append("mapnik_" + name)
        if self.config.USE_MAPSERVER:
            layers.append("mapserver")
        bbox = ",".join(str(c) for c in self.config.EXTENT_OSM)

        for layer in layers:
            if layer in self.config.TILECACHE_NOSEED_LAYERS:
                continue
            log.info("Seeding layer: %s", layer)
            # XXX add padding too? (-p option).
            call([seed_script, "-c", tc_config, "-b", bbox, layer,
                str(self.config.SEED_ZOOM_FROM),
                str(self.config.SEED_ZOOM_TO + 1)])

    def generate_clean(self):
        for d in os.listdir(self.cache_dir):
            # not using maybe_unlink to report errors.
            shutil.rmtree(join(self.cache_dir, d))


class ApacheConfig(Bundle):
    def system_setup(self):
        # Note: explicitly using apache2-mpm-prefork instead of
        # apache2-mpm-worker (the default) because it has some issues with
        # executing cgi scripts (such as mapserver)
        self.install_packages("apache2 apache2-mpm-prefork")

        link_source = join(
            "/etc/apache2/sites-enabled/" + self.config.APACHE_SERVER_NAME)
        link_target = join(self.project_dir, "apache", "apache.conf")

        if os.path.islink(link_source) and os.readlink(link_source) == link_target:
            return

        try:
            os.unlink(link_source)
        except OSError:
            pass
        os.symlink(link_target, link_source)


class OpenLayers(Bundle):
    def download(self):
        self.fetch_resources([
            ("http://openlayers.org/download/OpenLayers-2.10.tar.gz",
                "htdocs", "4fdb8d5bf731168a65add0fabe9234dd"),
        ])


class BundleExecutor(object):
    def __init__(self, options):
        self.oss_dir = os.path.abspath(os.path.dirname(__file__))
        self.project_dir = os.path.normpath(
            join(self.oss_dir, os.pardir))
        self.build_dir = join(self.project_dir, "build")
        make_dirs_as_project_owner(self.project_dir, self.build_dir)

        self.options = options
        self.config = self._parse_config()
        self.fetcher = Fetcher(self)

    def _parse_config(self):
        config = {
            'executor': self,
        }
        execfile(join(self.oss_dir, "default_config.py"), {}, config)

        config_path = join(self.project_dir, "config.py")
        if os.path.isfile(config_path):
            execfile(config_path, {}, config)

        config_local_path = join(self.project_dir, "config_local.py")
        if os.path.isfile(config_local_path):
            execfile(config_local_path, {}, config)

        log.debug("config: %s", config)

        # Convert the dict to an object, which is a bit more convenient to handle.
        class Struct:
            def __init__(self, d):
                self.__dict__.update(d)

        config = Struct(config)

        # Helper values

        try:
            if not hasattr(config, "EXTENT_OSM"):
                config.EXTENT_OSM = convert_bbox(
                    SRID_LATLON, config.SRID_OSM, config.EXTENT)
        except OSError:
            # proj might not be installed yet when using create_project.
            pass

        return config

    def get_bundle(self, name):
        bundles = [b for b in self.bundles if b.name == name]
        if len(bundles) != 1:
            raise Exception(
                "Not only one bundle found for name {1} (found: {1})".
                format(class_.__name__, len(bundles)))
        return bundles[0]

    def _create_bundles(self, bundle_name_to_only_load):
        self.bundles = []

        def instanciate_bundle(bundle_class_or_tuple):
            bundle_class = bundle_class_or_tuple
            args = [self]
            if isinstance(bundle_class_or_tuple, tuple):
                bundle_class = bundle_class_or_tuple[0]
                args.extend(bundle_class_or_tuple[1:])
            return bundle_class(*args)

        bundles_to_load = []

        if self.config.USE_SRTM:
            bundles_to_load.extend([instanciate_bundle(SRTMData)])

        for name in self.config.MAPNIK_INSTANCES:
            bundles_to_load.extend(
                [instanciate_bundle((MapnikConfig, name))])

        if self.config.USE_MAPNIK_OGCSERVER:
            bundles_to_load.extend([instanciate_bundle(MapnikOGCServer)])

        if self.config.USE_MAPSERVER:
            bundles_to_load.extend([instanciate_bundle(MapserverConfig)])

        if self.config.USE_TILECACHE:
            bundles_to_load.extend([instanciate_bundle(TileCache)])

        if self.config.USE_APACHE:
            bundles_to_load.extend([instanciate_bundle(ApacheConfig)])

        if self.config.USE_OPENLAYERS:
            bundles_to_load.extend([instanciate_bundle(OpenLayers)])

        # compute dependencies
        def add_dependencies(bundle, bundle_dependencies):
            class_dependencies = bundle.dependencies
            for c in class_dependencies:
                dep = instanciate_bundle(c)
                if dep.name not in set(b.name for b in bundle_dependencies):
                    bundle_dependencies.insert(0, dep)
                add_dependencies(dep, bundle_dependencies)

        log.debug("Bundles to load: %s", bundles_to_load)
        bundle_dependencies = []
        for b in bundles_to_load:
            add_dependencies(b, bundle_dependencies)

        log.debug("Bundle dependencies: %s", bundle_dependencies)

        if bundle_name_to_only_load:
            bundles_to_load = [b for b in bundle_dependencies + bundles_to_load if
                b.name == bundle_name_to_only_load]
            if not bundles_to_load:
                raise Exception("No bundle found for name: %r" %
                    bundle_name_to_only_load)
            log.info("Filtered bundles to load: %s", bundles_to_load)

            # Load dependencies again.
            bundle_dependencies = []
            for b in bundles_to_load:
                add_dependencies(b, bundle_dependencies)
            log.info("New dependencies: %s", bundle_dependencies)

        self.bundles = [instanciate_bundle(CoreBundle)] + bundle_dependencies + bundles_to_load
        log.debug("all_bundles: %s", self.bundles)

    def execute(self, command, bundle):
        self._create_bundles(bundle)

        ROOT_COMMANDS = ("system_setup", "system_setup_clean")
        if (os.geteuid() == 0 and command not in ROOT_COMMANDS and
            not "OSS_ALLOW_ROOT_ALL_COMMANDS" in os.environ):
            raise Exception("root user can only run {0} commands".format(
                " or ".join(ROOT_COMMANDS)))
        if os.geteuid() != 0 and command in ROOT_COMMANDS:
            raise Exception("commands {0} must be run with root user".format(
                " or ".join(ROOT_COMMANDS)))

        for bundle in self.bundles:
            try:
                method = getattr(bundle, command)
            except AttributeError:
                continue
            method()


if __name__ == "__main__":

    usage = "usage: %prog [options] command"

    parser = optparse.OptionParser(
        usage=usage, description="Standard commands: create_project, download, "
        "build, system_setup, load_data, generate")

    parser.add_option("--overwrite", action="store_true",
         default=False, help="Overwrite existing files without confirmation during project creation.")
    parser.add_option("-v", "--verbose", action="store_true",
         default=False, help="Print debug logging")

    (options, args) = parser.parse_args()

    if len(args) != 1:
        parser.print_help()
        sys.exit(1)

    logging.basicConfig(level=(logging.DEBUG if options.verbose else
                               logging.INFO))

    command = args[0]
    bundle = None
    if ":" in command:
        bundle, command = command.split(":")

    executor = BundleExecutor(options)
    executor.execute(command, bundle)
