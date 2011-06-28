OSM Server Setup
================

Introduction
------------

OSM Server Setup is a tool to help you setup and configure a server to display
OpenStreeMap data.
Configuration options have to be set, and a script takes care of configuring
and setting up the following components and tasks (the set of components to use
can be adjusted in the configuration):

* Install and configure a spatial PostgreSQL database
  (http://wiki.openstreetmap.org/wiki/PostGIS).
* Build the osm2pgsql tool for importing OSM data into the database
  (http://wiki.openstreetmap.org/wiki/Osm2pgsql).
* Download a set of .osm files and import them into the database.
* Download digital elevation model files (the ones from the NASA by default),
  import the data into PostgreSQL and generate a hill shading file
  (http://wiki.openstreetmap.org/wiki/SRTM).
* Create one or more Mapnik configuration files.
* Install Mapnik OGCServer to serve Mapnik through WMS
  (https://github.com/mapnik/OGCServer).
* Build Mapserver (http://mapserver.org/) and install it as CGI.
* Install a Mapserver configuration to display the OSM data
  (using http://code.google.com/p/mapserver-utils/ see
  http://trac.osgeo.org/mapserver/wiki/RenderingOsmData).
* Install a TileCache instance and configure it with Mapnik and Mapserver
  (http://tilecache.org/).
* Install OpenLayers and set up a demo .html file which can access the
  configured layers (http://openlayers.org/).
* Configure Apache.


Limitations
-----------

The script assumes it is running on a Debian 6 system.
It might work on Debian derivatives (e.g. Ubuntu) though.

There are a few hardcoded stuff that should rather be configurable.


Creating your project
---------------------

TODO: improve documentation.

mkdir myproject
cd myproject
git init
git submodule add git://github.com/spasche/osm-server-setup.git

# First, copy the sample config
cp osm-server-setup/config.py.sample config.py
# And edit it to suit your needs
nano config.py

python osm-server-setup/main.py create_project

git add .
git commit -m "Project created"

# If you get an error such as:
# psql: could not connect to server: Connection refused
# [...]
# subprocess.CalledProcessError: Command 'createuser -SDR gisuser' returned non-zero exit status 1
# start postgres and try again:
# /etc/init.d/postgresql start
# If you get an error such as
# createdb: database creation failed: ERROR:  new encoding (UTF8) is incompatible with the encoding of the template database (SQL_ASCII)
# run (warning: this drops all databases and starts from scratch):
# 
# pg_dropcluster --stop 8.4 main
# pg_createcluster --start -e UTF-8 8.4 main
#
sudo python osm-server-setup/main.py -v system_setup

python osm-server-setup/main.py -v download
python osm-server-setup/main.py -v build

# osm data
python osm-server-setup/main.py -v osmdata_osm_mapnik:load_data

# mapserver
python osm-server-setup/main.py -v mapserverconfig:load_data

# srtm
python osm-server-setup/main.py -v srtmdata:load_data

# Restart apache
sudo /etc/init.d/apache2 restart

# This will generate the tiles for tilecache
# (make sure that http://APACHE_SERVER_NAME/ is resolvable from your server)
python osm-server-setup/main.py -v tilecache:generate

# OpenLayers demo:
http://APACHE_SERVER_NAME/demo.html


Updating the OSM data with replication files
--------------------------------------------

This is based on the documentation from (see there for more details):
http://wiki.openstreetmap.org/wiki/Minutely_Mapnik
http://wiki.openstreetmap.org/wiki/Osmosis
http://wiki.openstreetmap.org/wiki/Osmosis/Detailed_Usage

First, you need to to have 'USE_OSMOSIS = True' in your config.py.
Run main.py with download and build commands to fetch all the requirements.

Edit data/osmosis/configuration.txt to select change interval and other settings.
{{{
# The URL of the directory containing change files.
baseUrl=http://planet.openstreetmap.org/hour-replicate

# Defines the maximum time interval in seconds to download in a single invocation.
# Setting to 0 disables this feature.
maxInterval = 0
}}}

Update the state file. You need to choose the right state URL:
wget -O http://planet.openstreetmap.org/hour-replicate/000/014/017.state.txt

# This reads the replication interval files, and puts them into data/osmosis/changes.osm.gz
python osm-server-setup/main.py -v osmosis:read_replication

# Loads the changes in the database.
python osm-server-setup/main.py -v osmdata_osm_mapnik:load_replication


Contact
-------

Author: Sylvain Pasche (sylvain.pasche@gmail.com)
