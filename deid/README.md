## CMS De-Identification
Empty tables (just like the identified ones) are created in the de-identified schema.  Then, following the [KUMC/HERON De-Identification Strategy](https://informatics.kumc.edu/work/wiki/DeIdentificationStrategy), the de-identified tables are loaded with data such that:
* Dates are shifted back in time a random number of days between 0 and 364.  All dates for a person are shifted the same amount to preserve relationships.
* Columns for county and zip code are set to NULL in the de-identified repository.
* `bene_id` and `msis_id` are replaced with a sequence number (though, they're already encrypted when we get them from CMS).
* Birth dates are shifted forward in time for anyone over the age of 89 such that they appear to be <= 89 (see [Guidance Regarding Methods for De-identification of Protected Health Information in Accordance with the Health Insurance Portability and Accountability Act (HIPAA) Privacy Rule](https://www.hhs.gov/hipaa/for-professionals/privacy/special-topics/de-identification/)).
* Age fields are adjusted such that none are over the age of 89.

Drop/build i2b2-style patient mapping table in the DEID schema:
```
# Drop/create deid mapping tables on the DEID side
cd deid
sqlplus $DEID_USER/$DEID_PASSWORD@$ORACLE_SID <<EOF

set echo on;

whenever sqlerror continue;
start i2b2_patient_mapping_init.sql;

EOF
```

Build some helper functions from the ETL code in the ID schema (`cms_source_cd` is the i2b2-style source string for the patient mapping).:
```
# Build functions, constants include in the ETL code
cd etl_i2b2/sql_scripts
sqlplus $ID_USER/$ID_PASSWORD@$ORACLE_SID <<EOF

set echo on;

whenever oserror exit 9;
whenever sqlerror exit sql.sqlcode;

define cms_source_cd='''${cms_source_cd}''';
define design_digest='''dummy'''

start cms_keys.pls

EOF
```

Build the mappings in the ID schema - `pmap_upload_id`, `cms_source_cd`, and `pmap_project_id` are i2b2 style columns for the patient mapping.
```
# Build mappings
cd deid
sqlplus $ID_USER/$ID_PASSWORD@$ORACLE_SID <<EOF

set echo on;

whenever oserror exit 9;
whenever sqlerror exit sql.sqlcode;

define cms_source_cd=${cms_source_cd};
define design_digest=dummy
start cms_mapping_init.sql;

define deid_schema=${DEID_USER};
define upload_id=${pmap_upload_id};
define cms_source_cd=${cms_source_cd};
define project_id=${pmap_project_id};
--bene_id_deid_start = previous year's max bene_id_deid + 1
define bene_id_deid_seq=${bene_id_deid_seq};
--msis_id_deid_seq_start =previous year's max msis_id_deid + 1 
define msis_id_deid_seq=${msis_id_deid_seq};

start bene_id_mapping.sql;
```

Indexes for the patient mapping:
```
# Build mapping indexes on the DEID side
cd deid
sqlplus $DEID_USER/$DEID_PASSWORD@$ORACLE_SID <<EOF

set echo on;

whenever sqlerror continue;
start i2b2_patient_mapping_index.sql;

EOF
```

Drop/create empty CMS tables in the DEID schema using `oracle_create.sql` generated by the staging code:
```
# Drop/create deid tables
sqlplus $DEID_USER/$DEID_PASSWORD@$ORACLE_SID <<EOF

set echo on;

whenever sqlerror continue;
start oracle_drop.sql; --This .sql is generated using parse_fts.py

whenever oserror exit 9;
whenever sqlerror exit sql.sqlcode;

start oracle_create.sql; --This .sql is generated using parse_fts.py
EOF
```

Load the de-identified tables from the identified copy:
```
# Run the deid script
cd deid

sqlplus $ID_USER/$ID_PASSWORD@$ORACLE_SID <<EOF

set echo on;

whenever oserror exit 9;
whenever sqlerror exit sql.sqlcode;

start cms_deid_init.sql;
start events.sql;
start hipaa_dob_shift.sql;

define deid_schema=${DEID_USER}; --Variable to replace "&&deid_schema" with the appropriate schema name

start cms_deid.sql;
EOF
```
### [i2b2\_patient\_mapping_init.sql](i2b2_patient_mapping_init.sql)
Drop/create i2b2-style patient mapping.

### [cms\_mapping_init.sql](cms_mapping_init.sql)
Initialize tables mapping identified and de-identified data.

### [bene\_id_mapping.sql](bene_id_mapping.sql)
Create a mapping between `bene_id` (or, `msis_id` + `state_cd`) and a sequence number representing the de-identified `bene_id`/`msis_id`.  The mapping also includes the per-person random date shift and the date of birth shift (or, NULL if no shift is required) and the date of birth shift noted above.  This file also builds an i2b2-style patient mapping table.

### [i2b2\_patient\_mapping_index.sql](i2b2_patient_mapping_index.sql)
Indexes for i2b2-style patient mappings.

### [cms\_deid_init.sql](cms_deid_init.sql)
Drop/create intermediate tables, sequences, etc. in preparation for de-identification.

### [events.sql](events.sql)
Any date field is considered an "event".  A table is created with person identifier columns (`bene_id` for Medicare tables or potentially `msis_is` + `state_cd` for Medicaid data) and a column for the date field.  This table is used to calculate how far to shift birth dates forward in time to make sure that no person appears to be over the age of 89.

### [hipaa\_dob_shift.sql](hipaa_dob_shift.sql)
Calculate the date of birth shift based on the "events" table.

### [deid_tests.sql](deid_tests.sql)
Tests to help make sure that de-identification was done properly.

### [cms_deid.sql](cms_deid.sql)
Populates the de-id CMS tables with de-identified data based on the date/age shifts calculated prior.

### [gen\_deid_sql.py](gen_deid_sql.py)
"Helper" script used to generate `cms_deid.sql` and `events.sql`.
```
Usage:
gen_deid_sql.py <path/to/oracle_create.sql> <path/to/table_column_desc.csv> <cms_deid_sql|date_events>
```
* `<path/to/oracle_create.sql>` is the path to the "create table" script created by the staging code.
* `<path/to/table_column_desc.csv>` is the path to the table/column description .csv file also created by the staging code.
* Option `cms_deid_sql` means the script generates the deid sql (cms_deid.sql)
* Option `date_events` means the script generates the events sql (events.sql)
