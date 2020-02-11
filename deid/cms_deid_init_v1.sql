-- deid_init.sql: Prepare for data de-identification
-- Copyright (c) 2020 University of Kansas Medical Center

whenever sqlerror continue;
drop table date_events;
drop table bene_id_mapping;
drop table patient_mapping;
whenever sqlerror exit;

-- All date fields from all tables
create table date_events (
  TABLE_NAME VARCHAR2(32),
  BENE_ID VARCHAR2(15),
  MSIS_ID VARCHAR2(32),
  STATE_CD VARCHAR2(2),
  COL_DATE VARCHAR2(32),
  DT DATE
  );
alter table date_events parallel (degree 12);

-- initial table generation (should only happen the first time)
create table bene_id_mapping (
  BENE_ID VARCHAR2(15),
  BENE_ID_DEID VARCHAR2(15),
  MSIS_ID VARCHAR2(32),
  STATE_CD VARCHAR2(2),
  DOB DATE,
  INDEX_DATE DATE,
  DATE_SHIFT_DAYS INTEGER
  );
alter table bene_id_mapping parallel (degree 12);

-- from i2b2 sources: crc_create_datamart_oracle.sql
create table patient_mapping (
    patient_ide         varchar2(200) not null,
    patient_ide_source  varchar2(50) not null,
    patient_num         number(38,0) not null,
    patient_ide_status  varchar2(50),
    project_id          varchar2(50) not null,
    upload_date         date,
    update_date         date,
    download_date       date,
    import_date         date,
    sourcesystem_cd     varchar2(50),
    upload_id           number(38,0),
    constraint patient_mapping_pk primary key(patient_ide, patient_ide_source, project_id)
 )
;