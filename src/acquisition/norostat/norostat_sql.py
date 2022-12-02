# standard library
import re

# third party
import mysql.connector

# first party
from .norostat_utils import *
import delphi.operations.secrets as secrets

# Column names:
# `release_date` :: release date as stated in the web page in the dateModified
#     span, displayed on the web page with the label "Page last updated:"
# `parse_time` :: time that we attempted to parse the data out of a downloaded
#     version of the web page; when the scraper is running, this may be similar
#     to a fetch time, but when loading in past versions that have been saved,
#     it probably won't mean the same thing; this is tracked (a) in case the
#     provided release date ever is out of date so that the raw data will still
#     be recorded and we can recover later on, and (b) to provide a record of
#     when parses/fetches happened; if there is a request for the data for a
#     particular `release_date` with no restrictions on `parse_time`, the
#     version with the latest `parse_time` should be selected
# (`release_date`, `parse_time`) :: uniquely identify a version of the table
# `measurement_type_id` :: "pointer" to an interned measurement_type string
# `measurement_type` :: the name of some column other than "Week" in the
#     data-table
# `location_id` :: "pointer" to an interned location string
# `location` :: a string containing the list of reporting states
# `week_id` :: "pointer" to an interned week string
# `week` :: a string entry from the "Week" column
# `value` :: an string entry from some column other than "Week" in the
#     data-table
# `new_value` :: an update to a `value` provided by a new version of the data
#     table: either a string representing an added or revised entry (or a
#     redundant repetition of a value retained from a past issue --- although
#     no such entries should be generated by the code in this file), or NULL
#     representing a deletion of a cell/entry from the table
#
# Tables:
# `norostat_raw_datatable_version_list` :: list of all versions of the raw
#     data-table that have ever been successfully parsed
# `<var>_pool` :: maps each encountered value of string `<var>` to a unique ID
#     `<var>_id`, so that the string's character data is not duplicated in the
#     tables on disk; serves a purpose similar to Java's interned string pool
# `norostat_raw_datatable_diffs` :: contains diffs between consecutive versions
#     of the raw data-table (when arranged according to the tuple
#     (`release_date`,`parse_time`) using lexicographical tuple ordering)
# `norostat_raw_datatable_parsed` :: a temporary table to hold the version of
#     the raw data-table (in long/melted format) to be recorded; uses string
#     values instead of interned string id's, so will need to be joined with
#     `*_pool` tables for operations with other tables
# `norostat_raw_datatable_previous` :: a temporary table to hold an
#     already-recorded version of the raw data-table with the latest
#     `release_date`, `parse_time` before those of the version to be recorded;
#     if there is no such version, this table will be empty (as if we recorded
#     an empty version of the table before all other versions); uses interned
#     string id's
# `norostat_raw_datatable_next` :: a temporary table to hold an
#     already-recorded version of the raw data-table with the earliest
#     `release_date`, `parse_time` after those of the version to be recorded;
#     if there is no such version, this table will not be created or used; uses
#     interned string id's

def ensure_tables_exist():
  (u, p) = secrets.db.epi
  cnx = mysql.connector.connect(user=u, password=p, database='epidata')
  try:
    cursor = cnx.cursor()
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS `norostat_raw_datatable_version_list` (
        `release_date` DATE NOT NULL,
        `parse_time` DATETIME(6) NOT NULL,
        PRIMARY KEY (`release_date`, `parse_time`)
      );
    ''')
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS `norostat_raw_datatable_measurement_type_pool` (
        `measurement_type_id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
        `measurement_type` NVARCHAR(255) NOT NULL UNIQUE KEY
      );
    ''')
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS `norostat_raw_datatable_location_pool` (
        `location_id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
        `location` NVARCHAR(255) NOT NULL UNIQUE KEY
      );
    ''')
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS `norostat_raw_datatable_week_pool` (
        `week_id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
        `week` NVARCHAR(255) NOT NULL UNIQUE KEY
      );
    ''')
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS `norostat_raw_datatable_diffs` (
        `release_date` DATE NOT NULL,
        `parse_time` DATETIME(6) NOT NULL,
        `measurement_type_id` INT NOT NULL,
        `location_id` INT NOT NULL,
        `week_id` INT NOT NULL,
        `new_value` NVARCHAR(255), -- allow NULL, with meaning "removed"
        FOREIGN KEY (`release_date`,`parse_time`) REFERENCES `norostat_raw_datatable_version_list` (`release_date`,`parse_time`),
        FOREIGN KEY (`measurement_type_id`) REFERENCES `norostat_raw_datatable_measurement_type_pool` (`measurement_type_id`),
        FOREIGN KEY (`location_id`) REFERENCES `norostat_raw_datatable_location_pool` (`location_id`),
        FOREIGN KEY (`week_id`) REFERENCES `norostat_raw_datatable_week_pool` (`week_id`),
        UNIQUE KEY (`measurement_type_id`, `location_id`, `week_id`, `release_date`, `parse_time`, `new_value`),
        PRIMARY KEY (`release_date`, `parse_time`, `measurement_type_id`, `location_id`, `week_id`)
        -- (the indices here are larger than the data, but reducing the key
        -- sizes and adding an id somehow seems to result in larger index sizes
        -- somehow)
      );
    ''')
    cnx.commit()
  finally:
    cnx.close()

def dangerously_drop_all_norostat_tables():
  (u, p) = secrets.db.epi
  cnx = mysql.connector.connect(user=u, password=p, database='epidata')
  try:
    cursor = cnx.cursor()
    # Drop tables in reverse order (to avoid foreign key related errors):
    cursor.execute('''
      DROP TABLE IF EXISTS `norostat_point_diffs`,
                           `norostat_point_version_list`,
                           `norostat_raw_datatable_diffs`,
                           `norostat_raw_datatable_week_pool`,
                           `norostat_raw_datatable_location_pool`,
                           `norostat_raw_datatable_measurement_type_pool`,
                           `norostat_raw_datatable_version_list`;
    ''')
    cnx.commit() # (might do nothing; each DROP commits itself anyway)
  finally:
    cnx.close()

def record_long_raw(long_raw):
  (long_raw_df, release_date, parse_time, location) = long_raw
  (u, p) = secrets.db.epi
  cnx = mysql.connector.connect(user=u, password=p, database='epidata')
  try:
    cursor = cnx.cursor()
    cnx.start_transaction(isolation_level='SERIALIZABLE')
    # Create, populate `norostat_raw_datatable_parsed`:
    cursor.execute('''
      CREATE TEMPORARY TABLE `norostat_raw_datatable_parsed` (
        `measurement_type` NVARCHAR(255) NOT NULL,
        `location` NVARCHAR(255) NOT NULL,
        `week` NVARCHAR(255) NOT NULL,
        `value` NVARCHAR(255) NOT NULL, -- forbid NULL; has special external meaning (see above)
        PRIMARY KEY (`measurement_type`, `location`, `week`)
      ) ENGINE=MEMORY;
    ''')
    cursor.executemany('''
      INSERT INTO `norostat_raw_datatable_parsed` (`week`,`measurement_type`,`value`,`location`)
      VALUES (%s, %s, %s, %s);
    ''', [(week, measurement_type, value, location) for
          (week, measurement_type, value) in long_raw_df[["week","measurement_type","value"]].astype(str).itertuples(index=False, name=None)
    ])
    # Create, populate `norostat_raw_datatable_previous`:
    cursor.execute('''
      CREATE TEMPORARY TABLE `norostat_raw_datatable_previous` (
        `measurement_type_id` INT NOT NULL,
        `location_id` INT NOT NULL,
        `week_id` INT NOT NULL,
        `value` NVARCHAR(255) NOT NULL, -- forbid NULL; has special external meaning (see above)
        -- would like but not allowed: FOREIGN KEY (`measurement_type_id`) REFERENCES `norostat_raw_datatable_measurement_type_pool` (`measurement_type_id`),
        -- would like but not allowed: FOREIGN KEY (`location_id`) REFERENCES `norostat_raw_datatable_location_pool` (`location_id`),
        -- would like but not allowed: FOREIGN KEY (`week_id`) REFERENCES `norostat_raw_datatable_week_pool` (`week_id`),
        PRIMARY KEY (`measurement_type_id`, `location_id`, `week_id`)
      ) ENGINE=MEMORY;
    ''')
    cursor.execute('''
      INSERT INTO `norostat_raw_datatable_previous` (`measurement_type_id`, `location_id`, `week_id`, `value`)
        SELECT `latest`.`measurement_type_id`, `latest`.`location_id`, `latest`.`week_id`, `latest`.`new_value`
        FROM `norostat_raw_datatable_diffs` AS `latest`
        -- Get the latest `new_value` by "group" (measurement_type, location, week)
        -- using the fact that there are no later measurements belonging to the
        -- same group (find NULL entries in `later`.{release_date,parse_time}
        -- in the LEFT JOIN below); if the latest `new_value` is NULL, don't
        -- include it in the result; it means that the corresponding cell/entry
        -- has been removed from the data-table:
        LEFT JOIN (
          SELECT * FROM `norostat_raw_datatable_diffs`
          WHERE (`release_date`,`parse_time`) <= (%s,%s)
        ) `later`
        ON `latest`.`measurement_type_id` = `later`.`measurement_type_id` AND
           `latest`.`location_id` = `later`.`location_id` AND
           `latest`.`week_id` = `later`.`week_id` AND
           (`latest`.`release_date`, `latest`.`parse_time`) <
             (`later`.`release_date`, `later`.`parse_time`)
        WHERE (`latest`.`release_date`, `latest`.`parse_time`) <= (%s, %s) AND
              `later`.`parse_time` IS NULL AND
              `latest`.`new_value` IS NOT NULL;
    ''', (release_date, parse_time, release_date, parse_time))
    # Find next recorded `release_date`, `parse_time` if any; create, populate
    # `norostat_raw_datatable_next` if there is such a version:
    cursor.execute('''
      SELECT `release_date`, `parse_time`
      FROM `norostat_raw_datatable_version_list`
      WHERE (`release_date`, `parse_time`) > (%s,%s)
      ORDER BY `release_date`, `parse_time`
      LIMIT 1
    ''', (release_date, parse_time))
    next_version_if_any = cursor.fetchall()
    expect_result_in(len, next_version_if_any, (0,1),
                     'Bug: expected next-version query to return a number of results in {}; instead have len & val ')
    if len(next_version_if_any) != 0:
      cursor.execute('''
        CREATE TEMPORARY TABLE `norostat_raw_datatable_next` (
          `measurement_type_id` INT NOT NULL,
          `location_id` INT NOT NULL,
          `week_id` INT NOT NULL,
          `value` NVARCHAR(255) NOT NULL, -- forbid NULL; has special external meaning (see above)
          -- would like but not allowed: FOREIGN KEY (`measurement_type_id`) REFERENCES `norostat_raw_datatable_measurement_type_pool` (`measurement_type_id`),
          -- would like but not allowed: FOREIGN KEY (`location_id`) REFERENCES `norostat_raw_datatable_location_pool` (`location_id`),
          -- would like but not allowed: FOREIGN KEY (`week_id`) REFERENCES `norostat_raw_datatable_week_pool` (`week_id`),
          PRIMARY KEY (`measurement_type_id`, `location_id`, `week_id`)
        ) ENGINE=MEMORY;
      ''')
      cursor.execute('''
        INSERT INTO `norostat_raw_datatable_next` (`measurement_type_id`, `location_id`, `week_id`, `value`)
          SELECT `latest`.`measurement_type_id`, `latest`.`location_id`, `latest`.`week_id`, `latest`.`new_value`
          FROM `norostat_raw_datatable_diffs` AS `latest`
          -- Get the latest `new_value` by "group" (measurement_type, location, week)
          -- using the fact that there are no later measurements belonging to the
          -- same group (find NULL entries in `later`.{release_date,parse_time}
          -- in the LEFT JOIN below); if the latest `new_value` is NULL, don't
          -- include it in the result; it means that the corresponding cell/entry
          -- has been removed from the data-table:
          LEFT JOIN (
            SELECT * FROM `norostat_raw_datatable_diffs`
            WHERE (`release_date`,`parse_time`) <= (%s, %s)
          ) `later`
          ON `latest`.`measurement_type_id` = `later`.`measurement_type_id` AND
             `latest`.`location_id` = `later`.`location_id` AND
             `latest`.`week_id` = `later`.`week_id` AND
             (`latest`.`release_date`, `latest`.`parse_time`) <
               (`later`.`release_date`, `later`.`parse_time`)
          WHERE (`latest`.`release_date`, `latest`.`parse_time`) <= (%s, %s) AND
             `later`.`parse_time` IS NULL AND
             `latest`.`new_value` IS NOT NULL -- NULL means value was removed
      ''', next_version_if_any[0]+next_version_if_any[0])
    # Register new version in version list:
    try:
      cursor.execute('''
        INSERT INTO `norostat_raw_datatable_version_list` (`release_date`, `parse_time`)
          VALUES (%s, %s)
      ''', (release_date, parse_time))
    except mysql.connector.errors.IntegrityError as e:
      raise Exception(['Encountered an IntegrityError when updating the norostat_raw_datatable_version_list table; this probably indicates that a version with the same `release_date` and `parse_time` was already added to the database; parse_time has limited resolution, so this can happen from populating the database too quickly when there are duplicate release dates; original error: ', e])
    # Add any new measurement_type, location, or week strings to the associated
    # string pools:
    cursor.execute('''
      INSERT INTO `norostat_raw_datatable_measurement_type_pool` (`measurement_type`)
        SELECT DISTINCT `measurement_type`
        FROM `norostat_raw_datatable_parsed`
        WHERE `measurement_type` NOT IN (
          SELECT `norostat_raw_datatable_measurement_type_pool`.`measurement_type`
          FROM `norostat_raw_datatable_measurement_type_pool`
        );
    ''')
    cursor.execute('''
      INSERT INTO `norostat_raw_datatable_location_pool` (`location`)
        SELECT DISTINCT `location`
        FROM `norostat_raw_datatable_parsed`
        WHERE `location` NOT IN (
          SELECT `norostat_raw_datatable_location_pool`.`location`
          FROM `norostat_raw_datatable_location_pool`
        );
    ''')
    cursor.execute('''
      INSERT INTO `norostat_raw_datatable_week_pool` (`week`)
        SELECT DISTINCT `week`
        FROM `norostat_raw_datatable_parsed`
        WHERE `week` NOT IN (
          SELECT `norostat_raw_datatable_week_pool`.`week`
          FROM `norostat_raw_datatable_week_pool`
        );
    ''')
    # Record diff: [newly parsed version "minus" previous version] (first,
    # record additions/updates, then record deletions):
    cursor.execute('''
      INSERT INTO `norostat_raw_datatable_diffs` (`measurement_type_id`, `location_id`, `week_id`, `release_date`, `parse_time`, `new_value`)
        SELECT `measurement_type_id`, `location_id`, `week_id`, %s, %s, `value`
        FROM `norostat_raw_datatable_parsed`
        LEFT JOIN `norostat_raw_datatable_measurement_type_pool` USING (`measurement_type`)
        LEFT JOIN `norostat_raw_datatable_location_pool` USING (`location`)
        LEFT JOIN `norostat_raw_datatable_week_pool` USING (`week`)
        WHERE (`measurement_type_id`, `location_id`, `week_id`, `value`) NOT IN (
          SELECT `norostat_raw_datatable_previous`.`measurement_type_id`,
                 `norostat_raw_datatable_previous`.`location_id`,
                 `norostat_raw_datatable_previous`.`week_id`,
                 `norostat_raw_datatable_previous`.`value`
          FROM `norostat_raw_datatable_previous`
        );
    ''', (release_date, parse_time))
    cursor.execute('''
      INSERT INTO `norostat_raw_datatable_diffs` (`measurement_type_id`, `location_id`, `week_id`, `release_date`, `parse_time`, `new_value`)
        SELECT `measurement_type_id`, `location_id`, `week_id`, %s, %s, NULL
        FROM `norostat_raw_datatable_previous`
        WHERE (`measurement_type_id`, `location_id`, `week_id`) NOT IN (
          SELECT `norostat_raw_datatable_measurement_type_pool`.`measurement_type_id`,
                 `norostat_raw_datatable_location_pool`.`location_id`,
                 `norostat_raw_datatable_week_pool`.`week_id`
          FROM `norostat_raw_datatable_parsed`
          LEFT JOIN `norostat_raw_datatable_measurement_type_pool` USING (`measurement_type`)
          LEFT JOIN `norostat_raw_datatable_location_pool` USING (`location`)
          LEFT JOIN `norostat_raw_datatable_week_pool` USING (`week`)
        );
    ''', (release_date, parse_time))
    # If there is an already-recorded next version, its diff is invalidated by
    # the insertion of the newly parsed version; delete the [next version
    # "minus" previous version] diff and record the [next version "minus" newly
    # parsed] diff:
    if len(next_version_if_any) != 0:
      cursor.execute('''
        DELETE FROM `norostat_raw_datatable_diffs`
        WHERE `release_date`=%s AND `parse_time`=%s;
      ''', next_version_if_any[0])
      cursor.execute('''
        INSERT INTO `norostat_raw_datatable_diffs` (`measurement_type_id`, `location_id`, `week_id`, `release_date`, `parse_time`, `new_value`)
          SELECT `measurement_type_id`, `location_id`, `week_id`, %s, %s, `value`
          FROM `norostat_raw_datatable_next`
          WHERE (`measurement_type_id`, `location_id`, `week_id`, `value`) NOT IN (
            SELECT
              `norostat_raw_datatable_measurement_type_pool`.`measurement_type_id`,
              `norostat_raw_datatable_location_pool`.`location_id`,
              `norostat_raw_datatable_week_pool`.`week_id`,
              `norostat_raw_datatable_parsed`.`value`
            FROM `norostat_raw_datatable_parsed`
            LEFT JOIN `norostat_raw_datatable_measurement_type_pool` USING (`measurement_type`)
            LEFT JOIN `norostat_raw_datatable_location_pool` USING (`location`)
            LEFT JOIN `norostat_raw_datatable_week_pool` USING (`week`)
          );
      ''', next_version_if_any[0])
      cursor.execute('''
        INSERT INTO `norostat_raw_datatable_diffs` (`measurement_type_id`, `location_id`, `week_id`, `release_date`, `parse_time`, `new_value`)
          SELECT `measurement_type_id`, `location_id`, `week_id`, %s, %s, NULL
          FROM `norostat_raw_datatable_parsed`
          LEFT JOIN `norostat_raw_datatable_measurement_type_pool` USING (`measurement_type`)
          LEFT JOIN `norostat_raw_datatable_location_pool` USING (`location`)
          LEFT JOIN `norostat_raw_datatable_week_pool` USING (`week`)
          WHERE (`measurement_type_id`, `location_id`, `week_id`) NOT IN (
            SELECT `norostat_raw_datatable_next`.`measurement_type_id`,
                   `norostat_raw_datatable_next`.`location_id`,
                   `norostat_raw_datatable_next`.`week_id`
            FROM `norostat_raw_datatable_next`
          );
      ''', next_version_if_any[0])
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS `norostat_point_version_list` (
        `release_date` DATE NOT NULL,
        `parse_time` DATETIME(6) NOT NULL,
        FOREIGN KEY (`release_date`,`parse_time`) REFERENCES `norostat_raw_datatable_version_list` (`release_date`,`parse_time`),
        PRIMARY KEY (`release_date`, `parse_time`)
      );
    ''')
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS `norostat_point_diffs` (
        `release_date` DATE NOT NULL,
        `parse_time` datetime(6) NOT NULL,
        `location_id` INT NOT NULL,
        `epiweek` INT NOT NULL,
        `new_value` NVARCHAR(255), -- allow NULL, with meaning "removed"
        FOREIGN KEY (`release_date`,`parse_time`) REFERENCES `norostat_point_version_list` (`release_date`,`parse_time`),
        FOREIGN KEY (`location_id`) REFERENCES norostat_raw_datatable_location_pool (`location_id`),
        UNIQUE KEY (`location_id`, `epiweek`, `release_date`, `parse_time`, `new_value`),
        PRIMARY KEY (`release_date`, `parse_time`, `location_id`, `epiweek`)
      );
    ''')
    cnx.commit() # (might do nothing; each statement above takes effect and/or commits immediately)
  finally:
    cnx.close()

def update_point():
  (u, p) = secrets.db.epi
  cnx = mysql.connector.connect(user=u, password=p, database='epidata')
  try:
    cursor = cnx.cursor()
    cnx.start_transaction(isolation_level='serializable')
    cursor.execute('''
      SELECT `release_date`, `parse_time`, `measurement_type`, `location_id`, `week`, `new_value`
      FROM `norostat_raw_datatable_diffs`
      LEFT JOIN `norostat_raw_datatable_measurement_type_pool` USING (`measurement_type_id`)
      LEFT JOIN `norostat_raw_datatable_week_pool` USING (`week_id`)
      WHERE (`release_date`, `parse_time`) NOT IN (
        SELECT `norostat_point_version_list`.`release_date`,
               `norostat_point_version_list`.`parse_time`
        FROM `norostat_point_version_list`
      );
    ''')
    raw_datatable_diff_selection = cursor.fetchall()
    prog = re.compile(r"[0-9]+-[0-9]+$")
    point_diff_insertion = [
        (release_date, parse_time, location_id,
         season_db_to_epiweek(measurement_type, week),
         int(new_value_str) if new_value_str is not None else None
        )
        for (release_date, parse_time, measurement_type, location_id, week, new_value_str)
        in raw_datatable_diff_selection
        if prog.match(measurement_type) is not None and
           new_value_str != ""
    ]
    cursor.execute('''
      INSERT INTO `norostat_point_version_list` (`release_date`, `parse_time`)
        SELECT DISTINCT `release_date`, `parse_time`
        FROM `norostat_raw_datatable_version_list`
        WHERE (`release_date`, `parse_time`) NOT IN (
          SELECT `norostat_point_version_list`.`release_date`,
                 `norostat_point_version_list`.`parse_time`
          FROM `norostat_point_version_list`
        );
    ''')
    cursor.executemany('''
      INSERT INTO `norostat_point_diffs` (`release_date`, `parse_time`, `location_id`, `epiweek`, `new_value`)
      VALUES (%s, %s, %s, %s, %s)
    ''', point_diff_insertion)
    cnx.commit()
  finally:
    cnx.close()

# note there are more efficient ways to calculate diffs without forming ..._next table
# todo give indices names
# todo trim pool functionality for if data is deleted?
# todo make classes to handle pool, keyval store, and diff table query formation
# todo test mode w/ rollback
# todo record position of rows and columns in raw data-table (using additional diff tables)
# todo consider measurement index mapping <measurement_type_id, location_id, week_id> to another id
# todo add fetch_time to version list
# xxx replace "import *"'s
# xxx should cursor be closed?
# xxx is cnx auto-closed on errors?
# xxx drop temporary tables?
# fixme time zone issues
