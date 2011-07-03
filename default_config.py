DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "gis"
DB_USER = "gisuser"
DB_PASSWORD = "override-me"

EXTENT = (5.94, 45.70, 10.54, 47.90)
# Coordinate system to use for storing OSM data.
# Note: might not work if changed to something other than spherical mercator
SRID_OSM = 900913

USE_OSMOSIS = False

OSM2PGSQL_SVN_URL = "http://svn.openstreetmap.org/applications/utils/export/osm2pgsql/"
OSM2PGSQL_SVN_REVISION = "26061" # 2011-05-04 21:51:45 +0200 (Wed, 04 May 2011)
# Set this to True if you are loading large data, or if you plan to load diffs.
OSM2PGSQL_SLIM_MODE = False
OSM_DATA_URLS = ["http://download.geofabrik.de/osm/europe/switzerland.osm.bz2"]
# This can be used to use another style file than the upstream default.style.
# Key should be osm_mapserver or osm_mapnik, value is the path to the style file.
OSM_DATA_STYLE_PATH = {}

USE_SRTM = True
# List of hgt.zip URLs that should be downloaded instead of the NASA ones.
# The list should contain tuples (url, target_filename), where target_filename
# should follow the NASA naming convention: {N,S}NNN{E,W}NNN.hgt.zip file
# which contains a {N,S}NNN{E,W}NNN.hgt file inside.
SRTM_HGT_URLS = [
    #("http://example.com/myfile.zip", "N46E006.hgt.zip"),
]
# Set this to a value greater than 0 to resize the .hgt file to the given
# dimension (same for height and width). NASA .hgt tiles have a dimension
# of 1201 x 1201. This parameter can be useful if you are using tiles that
# have a higher resolution and should be simplified.
SRTM_RESIZE_DIMENSION = -1
# Whether to generate a hill shading .tif image from the elevation files.
# (See http://wiki.openstreetmap.org/wiki/Hillshading_with_Mapnik)
# This requires USE_SRTM to be True
USE_HILLSHADING = True

# Set this to an empty list to disable Mapnik
MAPNIK_INSTANCES = ["upstream", "custom"]
MAPNIK_SVN_URL = "http://svn.openstreetmap.org/applications/rendering/mapnik"
MAPNIK_SVN_REVISION = "26022" # 2011-05-21 00:46:09 +0200 (Sat, 21 May 2011)

USE_MAPNIK_OGCSERVER = True

USE_MAPSERVER = True
MAPSERVER_SVN_URL = "http://mapserver-utils.googlecode.com/svn/trunk/"
MAPSERVER_SVN_REVISION = "79" # 2011-05-04 13:36:55 +0200 (Wed, 04 May 2011)

USE_TILECACHE = True
TILECACHE_NOSEED_LAYERS = set()
# Zoom levels to generate (inclusive).
SEED_ZOOM_FROM = 1
SEED_ZOOM_TO = 10

USE_APACHE = True
APACHE_SERVER_NAME = "carto"
APACHE_SERVER_ALIASES = []

USE_OPENLAYERS = True
