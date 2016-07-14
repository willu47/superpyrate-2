""" Runs an integrated pipeline from raw zip file to database tables.

This mega-pipeline is constructed out of three smaller pipelines and now brings
together tasks which:

1. Unzip individual AIS archives and output the csv files
2. Validate each of the csv files, processing using a derived version
   of the pyrate code, outputting vaidated csv files
3. Using the postgres `copy` command, ingest the validated data directly into
   the database

Because the exact contents of the zipped archives are unknown until they are
unzipped, tasks are spawned dynamically.

.. graphviz::

    digraph pipeline {
        GetZipArchive [label="GetZipArchive", href="superpyrate.html#superpyrate.pipeline.GetZipArchive", target="_top", shape=box];
        GetFolderOfArchives [label="GetFolderOfArchives", href="superpyrate.html#superpyrate.pipeline.GetFolderOfArchives", target="_top", shape=box];
        UnzippedArchive [label="UnzippedArchive", href="superpyrate.html#superpyrate.pipeline.UnzippedArchive", target="_top", shape=diamond];
        UnzippedArchive -> GetZipArchive;
        ProcessCsv [label="ProcessCsv", href="superpyrate.html#superpyrate.pipeline.ProcessCsv", target="_top", shape=diamond, colorscheme=dark26, color=4, style=filled];
        ProcessCsv -> UnzippedArchive;
        ProcessCsv -> ValidMessages [arrowhead=dot,arrowtail=dot]
        GetCsvFile [label="GetCsvFile", href="superpyrate.html#superpyrate.pipeline.GetCsvFile", target="_top", shape=box];
        ValidMessages [label="ValidMessages", href="", target="_top", shape=diamond];
        ValidMessages -> GetCsvFile;
        ValidMessages -> fs [arrowhead=odot];
        ValidMessagesToDatabase [label="ValidMessagesToDatabase", href="superpyrate.html#superpyrate.pipeline.ValidMessagesToDatabase", target="_top", shape=diamond];
        ValidMessagesToDatabase -> ValidMessages;
        ValidMessagesToDatabase -> db [arrowhead=odot];
        LoadCleanedAIS [label="LoadCleanedAIS", href="superpyrate.html#superpyrate.pipeline.LoadCleanedAIS", target="_top", shape=diamond];
        LoadCleanedAIS -> ValidMessagesToDatabase;
        LoadCleanedAIS -> db [arrowhead=odot];
        WriteCsvToDb [label="WriteCsvToDb", href="superpyrate.html#superpyrate.pipeline.WriteCsvToDb", target="_top", shape=diamond, colorscheme=dark26, color=4, style=filled];
        WriteCsvToDb -> UnzippedArchive;
        WriteCsvToDb -> LoadCleanedAIS [arrowhead=dot,arrowtail=dot];
        ProcessZipArchives [label="ProcessZipArchives", href="superpyrate.html#superpyrate.pipeline.ProcessZipArchives", target="_top", shape=diamond, colorscheme=dark26, color=3, style=filled];
        ProcessZipArchives -> GetFolderOfArchives;
        ProcessZipArchives -> ProcessCsv [arrowhead=dot, arrowtail=dot];
        ProcessZipArchives -> WriteCsvToDb [arrowhead=dot, arrowtail=dot];
        RunQueryOnTable [label="RunQueryOnTable", href="superpyrate.html#superpyrate.pipeline.RunQueryOnTable", target="_top", shape=diamond];
        RunQueryOnTable -> db [arrowhead=odot];
        MakeAllIndices [label="MakeAllIndices", href="superpyrate.html#superpyrate.pipeline.MakeAllIndices", target="_top", shape=diamond, colorscheme=dark26, color=2, style=filled];
        MakeAllIndices -> RunQueryOnTable [arrowhead=dot, arrowtail=dot];
        MakeAllIndices -> ProcessZipArchives;
        ClusterAisClean [label="ClusterAisClean", href="superpyrate.html#superpyrate.pipeline.ClusterAisClean", target="_top", shape=diamond, colorscheme=dark26, color=1, style=filled];
        ClusterAisClean -> MakeAllIndices;
        ClusterAisClean -> db [arrowhead=odot];

        db [label="database", shape=cylinder];
        fs [label="filesystem", shape=folder];
    }

Entry points
============

It is not necessary to run the entire pipeline, although there is little harm
in doing so, as luigi manages the process so that individual tasks are
idempotent.  This means that a task only runs if required.  Luigi only runs the
tasks necessary to produce the files which are required for the specified entry
point.

For example, to run the entire pipeline, producing a full ingested and clustered
database, run::

    luigi --module opepr.message ClusterAisClean
          --workers 12

If only the validated csv files are required, run::

    luigi --module opepr.message ProcessZipArchives
          --workers 12
          --folder-of-zips /folder/of/zips/
          --shell-script /path/to/unzip/shell/script
          --with_db

Working folder
==============
The working folder ``LUIGIWORK`` must contain two subfolders - files and tmp.
The ``files`` subfolder contains the ``unzipped`` and ``cleancsv`` folders,
with all of the respective temporary files stored within.
The ``tmp`` subfolder contains ``processcsv``, ``writecsv``, ``archives`` and ``database``
folders and contains files which are generated by the tasks which do not produce
an actual file as output, rather spawn child-tasks.

Environment Variables
=====================

``LUIGIWORK``
    the working folder for the files

``DBHOSTNAME``
    hostname for the database e.g. localhost

``DBNAME``
    the name of the database

``DBUSER``
    the name of the user with access to the database

``DBUSERPASS``
    the password of the database user
"""
import luigi
from luigi.contrib.external_program import ExternalProgramTask
from luigi.postgres import CopyToTable, PostgresQuery
from luigi import six
from luigi.util import requires
from superpyrate.tasks import produce_valid_csv_file
from pyrate.repositories.aisdb import AISdb
import csv
import psycopg2
import logging
import os
LOGGER = logging.getLogger('luigi-interface')
LOGGER.setLevel(logging.INFO)


def setup_working_folder():
    """Setup the working folder structure for the entire luigi pipeline

    The working folder ('LUIGIWORK') must contain two subfolders - files and tmp.
    The `files` subfolder contains the `unzipped` and `cleancsv` folders,
    with all of the respective temporary files stored within.
    The `tmp` subfolder contains `processcsv`, `writecsv`, `archives` and `database`
    folders and contains files which are generated by the tasks which do not produce
    an actual file as output, rather spawn child-tasks.
    """

    working_folder = get_working_folder()
    folder_structure = {'files': ['unzipped', 'cleancsv'],
                        'tmp': ['processcsv', 'writecsv',
                                'archives', 'database', 'countraw']}
    for folder, subfolders in folder_structure.items():
        [os.makedirs(os.path.join(working_folder, folder, subfolder),
                     exist_ok=True) for subfolder in subfolders]


def get_environment_variable(name):
    """Tries to access an environment variable, and handles the error by replacing
    the value with a dummy value (an empty string)
    """
    assert isinstance(name, str)
    try:
        envvar = os.environ[name]
    except KeyError:
        envvar = ''
        LOGGER.error("{} environment variable not found, using default".format(name))
    return envvar


def get_working_folder(folder_of_zips=None):
    """

    Arguments
    =========
    folder_of_zips : str
        The absolute path of the folder of zips e.g. ``/tests/fixtures/testais/``

    Returns
    =======
    working_folder : str
        The path of the working folder.  This is either set by the environment
        variable ``LUIGIWORK``, or if empty is computed from the arguments
    """
    environment_variable = get_environment_variable('LUIGIWORK')
    if environment_variable:
        working_folder = environment_variable
    else:
        if folder_of_zips is None:
            raise RuntimeError("No working folder defined")
        else:
            working_folder = os.path.dirname(os.path.dirname(folder_of_zips))
    return working_folder


class GetZipArchive(luigi.ExternalTask):
    """Returns a zipped archive as a luigi.file.LocalTarget
    """
    zip_file = luigi.Parameter(description='The file path of the archive to unzip')

    def output(self):
        return luigi.file.LocalTarget(self.zip_file)


class GetFolderOfArchives(luigi.ExternalTask):
    """Returns the folder of zipped archives as a luigi.file.LocalTarget
    """
    folder_of_zips = luigi.Parameter()

    def run(self):
        assert isinstance(folder_of_zips, str)
        folder_of_zips = folder_of_zips.rstrip("\\")

    def output(self):
        return luigi.file.LocalTarget(self.folder_of_zips)


class UnzippedArchive(ExternalProgramTask):
    """Unzips the zipped archive into a folder of AIS csv format files the same
    name as the original file

    Arguments
    =========
    zip_file : str
        The absolute path of the zipped archives

    """
    zip_file = luigi.Parameter(description='The file path of the archive to unzip')

    def requires(self):
        return GetZipArchive(self.zip_file)

    def program_args(self):
        """Runs 7zip to extract the archives of AIS files

        Notes
        =====
        ``e``
            Unzip all ignoring folder structure (i.e. to highest level)
        ``-o``
            Output folder
        ``-y``
            Answer yes to everything
        """
        # Removes the file extension to give a folder name as the output target
        output_folder = self.output().fn
        LOGGER.info('Unzipping {0} to {1}'.format(self.input().fn,
                                                  output_folder))
        return ['7za', 'e' , self.input().fn, '-o{}'.format(output_folder), '-y']

    def output(self):
        """Outputs the files into a folder of the same name as the zip file

        The files are placed in a subdirectory of ``LUIGIWORK`` called ``files/unzipped``
        """
        out_root_dir = os.path.splitext(self.input().fn)[0]
        _, out_folder_name = os.path.split(out_root_dir)
        rootdir = get_working_folder()
        output_folder = os.path.join(rootdir,'files', 'unzipped', out_folder_name)
        # LOGGER.debug("Unzipped {}".format(output_folder))
        return luigi.file.LocalTarget(output_folder)


class ProcessCsv(luigi.Task):
    """

    Yields
    ======
    `ValidMessages`
    """
    zip_file = luigi.Parameter()

    def requires(self):
        return UnzippedArchive(self.zip_file)

    def run(self):
        list_of_csvpaths = []
        LOGGER.debug("Processing csvs from {}".format(self.input().fn))
        for csvfile in os.listdir(self.input().fn):
            if os.path.splitext(csvfile)[1] == '.csv':
                list_of_csvpaths.append(os.path.join(self.input().fn, csvfile))

        yield [ValidMessages(csvfilepath) for csvfilepath in list_of_csvpaths]

        with self.output().open('w') as outfile:
            outfile.write("\n".join(list_of_csvpaths))

    def output(self):
        """Dummy files are placed in a folder of the same name as the zip file

        The files are placed in a subdirectory of ``LUIGIWORK`` called
        ``tmp/processcsv``
        """
        filename = os.path.split(self.zip_file)[1]
        name = os.path.splitext(filename)[0]
        rootdir = get_working_folder()
        path = os.path.join(rootdir, 'tmp','processcsv', name)
        return luigi.file.LocalTarget(path)


class GetCsvFile(luigi.ExternalTask):
    """
    """
    csvfile = luigi.Parameter()

    def output(self):
        return luigi.file.LocalTarget(self.csvfile)


class ValidMessages(luigi.Task):
    """ Takes AIS messages and runs validation functions, generating valid csv
    files in folder called 'cleancsv' at the same level as unzipped_ais_path
    """
    csvfile = luigi.Parameter()

    def requires(self):
        return GetCsvFile(self.csvfile)

    def run(self):
        LOGGER.debug("Processing {}.  Output to: {}".format(self.input().fn, self.output().fn))
        infile = self.input().fn
        outfile = self.output().fn
        produce_valid_csv_file(infile, outfile)

    def output(self):
        """Validated files are named as the original csv file

        The files are placed in a subdirectory of ``LUIGIWORK`` called
        ``files/cleancsv``
        """
        name = os.path.basename(self.input().fn)
        rootdir = get_working_folder()
        path = os.path.join(rootdir, 'files','cleancsv', name)
        clean_file_out = os.path.join(path)
        LOGGER.info("Clean file saved to {}".format(clean_file_out))
        return luigi.file.LocalTarget(clean_file_out)


class ValidMessagesToDatabase(CopyToTable):
    """Writes the valid csv files to the postgres database

    Parameters
    ==========
    original_csvfile : luigi.Parameter
        The raw csvfile containing AIS data
    """

    original_csvfile = luigi.Parameter()

    # resources = {'postgres': 1}

    null_values = (None,"")
    column_separator = ","

    host = get_environment_variable('DBHOSTNAME')
    database = get_environment_variable('DBNAME')
    user = get_environment_variable('DBUSER')
    password = get_environment_variable('DBUSERPASS')
    table = "ais_clean"

    cols = ['MMSI','Time','Message_ID','Navigational_status','SOG',
               'Longitude','Latitude','COG','Heading','IMO','Draught',
               'Destination','Vessel_Name',
               'ETA_month','ETA_day','ETA_hour','ETA_minute']
    columns = [x.lower() for x in cols]
    # LOGGER.debug("Columns: {}".format(columns))

    def requires(self):
        return ValidMessages(self.original_csvfile)

    def rows(self):
        """Return/yield tuples or lists corresponding to each row to be inserted.

        Yields
        ======
        row : iterable
        """
        with self.input().open('r') as csvfile:
            reader = csv.reader(csvfile)
            for row in reader:
                yield row
                # LOGGER.debug(line)
                # yield [x for x in line.strip('\n').split(',') ]

    def copy(self, cursor, clean_file):
        if isinstance(self.columns[0], six.string_types):
            column_names = self.columns
        elif len(self.columns[0]) == 2:
            column_names = [c[0] for c in self.columns]
        else:
            raise Exception('columns must consist of column strings or (column string, type string) tuples (was %r ...)' % (self.columns[0],))
        LOGGER.debug(column_names)
        sql = "COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)".format(self.table, ",".join(column_names), clean_file)
        LOGGER.debug("File: {}".format(clean_file))
        cursor.copy_expert(sql, clean_file)

    def run(self):
        """Inserts data generated by rows() into target table.

        If the target table doesn't exist, self.create_table will be called
        to attempt to create the table.


        """
        if not (self.table and self.columns):
            raise Exception("table and columns need to be specified")

        connection = self.output().connect()

        with self.input().open('r') as csvfile:
            for attempt in range(2):
                try:
                    cursor = connection.cursor()
                    # self.init_copy(connection)
                    self.copy(cursor, csvfile)
                    # self.post_copy(connection)
                except psycopg2.ProgrammingError as e:
                    if e.pgcode == psycopg2.errorcodes.UNDEFINED_TABLE and attempt == 0:
                        # if first attempt fails with "relation not found", try creating table
                        LOGGER.info("Creating table %s", self.table)
                        connection.reset()
                        self.create_table(connection)
                    else:
                        raise
                else:
                    break

        # mark as complete in same transaction
        self.output().touch(connection)
        # commit and clean up
        connection.commit()
        connection.close()


class LoadCleanedAIS(CopyToTable):
    """Update ``ais_sources`` table with name of CSV file processed

    After the valid csv files are successfully written to the database,
    this function updates the ``sources`` table with the name of the file
    which has been written
    """

    csvfile = luigi.Parameter()

    # resources = {'postgres': 1}

    null_values = (None,"")
    column_separator = ","

    host = get_environment_variable('DBHOSTNAME')
    database = get_environment_variable('DBNAME')
    user = get_environment_variable('DBUSER')
    password = get_environment_variable('DBUSERPASS')
    table = "ais_sources"

    def requires(self):
        return ValidMessagesToDatabase(self.csvfile)

    def run(self):
        # Prepare source data to add to ais_sources
        source_data = {'filename': os.path.basename(self.csvfile),
                       'ext': os.path.splitext(self.csvfile)[1],
                       'invalid': 0,
                       'clean': 0,
                       'dirty': 0,
                       'source': 0}

        columns = '(' + ','.join([c.lower() for c in source_data.keys()]) + ')'

        connection = self.output().connect()
        cursor = connection.cursor()
        with cursor:
            tuplestr = "(" + ",".join("%({})s".format(i) for i in source_data.keys()) + ")"
            cursor.execute("INSERT INTO " + self.table + " "+ columns + " VALUES "+ tuplestr, source_data)

        # mark as complete
        self.output().touch(connection)

        # commit and clean up
        connection.commit()
        connection.close()


@requires(UnzippedArchive)
class WriteCsvToDb(luigi.Task):
    """Dynamically spawns :py:class:`LoadCleanedAIS` to load valid csvs into the database
    """
    def run(self):
        list_of_csvpaths = []
        LOGGER.debug("Writing csvs from {}".format(self.input().fn))
        for csvfile in os.listdir(self.input().fn):
            if os.path.splitext(csvfile)[1] == '.csv':
                list_of_csvpaths.append(os.path.join(self.input().fn, csvfile))
        yield [LoadCleanedAIS(csvfilepath) for csvfilepath in list_of_csvpaths]

        with self.output().open('w') as outfile:
            outfile.write("\n".join(list_of_csvpaths))

    def output(self):
        filename = os.path.split(self.zip_file)[1]
        name = os.path.splitext(filename)[0]
        rootdir = get_working_folder()
        path = os.path.join(rootdir, 'tmp','writecsv', name)
        return luigi.file.LocalTarget(path)


class ProcessZipArchives(luigi.Task):
    """Dynamically spawns :py:class:`WriteCsvToDb` or :py:class:`ProcessCsv` depending on database

    Parameters
    ==========
    with_db : bool
        Indicate whether a database is available for writing csv files

    Yields
    ======
    :py:class:`WriteCsvToDb`

    :py:class:`ProcessCsv`

    """
    folder_of_zips = luigi.Parameter(significant=True)
    with_db = luigi.BoolParameter(significant=False)

    def requires(self):
        return GetFolderOfArchives(self.folder_of_zips)

    def run(self):
        """
        """
        setup_working_folder()

        archives = []
        LOGGER.warn("Database flag is {}".format(self.with_db))
        LOGGER.debug("ProcessZipArchives input is: {}".format(self.input().fn))
        print(self.input().fn)
        filesystem = self.input().fs
        list_of_archives = [x for x in filesystem.listdir(self.input().fn)]
        LOGGER.debug(list_of_archives)
        for archive in list_of_archives:
            if os.path.splitext(archive)[1] == '.zip':
                archives.append(archive)
        LOGGER.debug(archives)
        if self.with_db is True:
            yield [WriteCsvToDb(arc) for arc in archives]
        else:
            yield [ProcessCsv(arc) for arc in archives]
        with self.output().open('w') as outfile:
            for arc in list_of_archives:
                outfile.write("{}\n".format(arc))

    def output(self):
        LOGGER.debug("Folder of zips: {} with db {}".format(self.folder_of_zips,
                                                            self.with_db))
        out_folder_name = 'archive_{}'.format(os.path.basename(self.folder_of_zips))
        root_folder = get_working_folder()
        return luigi.file.LocalTarget(os.path.join(root_folder,
                                                   'tmp',
                                                   'archives',
                                                   out_folder_name))


class RunQueryOnTable(PostgresQuery):
    """Runs a query on a table in the database

    Used for passing in utility type queries to the database such as creation
    of indices etc.

    Parameters
    ==========
    query : str
        A legal sql query
    table : str, default='ais_clean'
        A table on which to run the query
    """
    query = luigi.Parameter()
    table = luigi.Parameter(default='ais_clean')
    update_id = luigi.Parameter()

    host = get_environment_variable('DBHOSTNAME')
    database = get_environment_variable('DBNAME')
    user = get_environment_variable('DBUSER')
    password = get_environment_variable('DBUSERPASS')


@requires(ProcessZipArchives)
class MakeAllIndices(luigi.Task):
    """Creates the indices required for a specified table

    The list of indices are derived from the table specification in
    :py:mod:`pyrate`

    Parameters
    ==========
    table : str, default='ais_clean'
    """

    table = luigi.Parameter(default='ais_clean')
    # with_db = True

    def run(self):
        """
        """
        options = {}
        options['host'] = get_environment_variable('DBHOSTNAME')
        options['db'] = get_environment_variable('DBNAME')
        options['user'] = get_environment_variable('DBUSER')
        options['pass'] = get_environment_variable('DBUSERPASS')

        db = AISdb(options)
        with db:
            if self.table == 'ais_clean':
                indices = db.clean_db_spec['indices']
            elif self.table == 'ais_dirty':
                indices = db.dirty_db_spec['indices']
            else:
                raise NotImplemented('Table not implemented or incorrect')

        queries = []
        for idx, cols in indices:
            idxn = self.table.lower() + "_" + idx
            sql = ("CREATE INDEX \"" +
                   idxn +"\" ON \""+ self.table + "\" USING btree (" +
                   ','.join(["\"{}\"".format(s.lower()) for s in cols]) +")")
            update_id = self.__class__.__name__ + idxn
            queries.append((sql, update_id))


        yield [RunQueryOnTable(query, self.table, up_id) for query, up_id in queries]

        with self.output().open('w') as outfile:
            outfile.write(self.table)

    def output(self):
        filename = 'create_{}_indexes.txt'.format(self.table)
        rootdir = get_working_folder()
        path = os.path.join(rootdir, 'tmp','database', filename)
        return luigi.file.LocalTarget(path)


@requires(MakeAllIndices)
class ClusterAisClean(PostgresQuery):
    """Clusters the ais_clean table over the disk on the mmsi index
    """
    host = get_environment_variable('DBHOSTNAME')
    database = get_environment_variable('DBNAME')
    user = get_environment_variable('DBUSER')
    password = get_environment_variable('DBUSERPASS')
    table = "ais_clean"
    query = 'CLUSTER VERBOSE ais_clean USING ais_clean_mmsi_idx;'
