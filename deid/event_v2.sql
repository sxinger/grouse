-- Copyright (c) 2020 University of Kansas Medical Center

/*a more elegant solution using cursor, dynamic sql and for loop, need to test efficiency*/

-- make sure the sql statement is correct
--select 'INSERT INTO /*+ APPEND*/ date_events'
--       || ' SELECT /*+ PARALLEL('|| table_name || ',12) */ ' 
--       || '''' || table_name || '''' || ' TABLE_NAME'
--       || ', bene_id, null msis_id, null state_cd'
--       || ', '|| '''' || column_name || '''' || ' COL_DATE'
--       || ',' || column_name || ' DT'
--       || ' from ' || owner || '.' || table_name 
--       || ' where ' || column_name || ' is not null'
--from all_tab_columns 
--where owner = '$$new_CMS_schema1' and rownum <=4;


declare
  sql_stmt VARCHAR2(4000);
  /*use magic words to identify date columns across all tables*/
  cursor date_col is
  select owner
        ,table_name
        ,column_name
        ,case when table_name like 'MAX%' then 1        -- special identifiers in medicaid table names (may need to specify mannually)
              else 0
         end as msis_ind
   from all_tab_columns
   where owner in ('$$new_CMS_schema1','$$new_CMS_schema2') and 
         data_type = 'DATE';
begin
  for rec in date_col
  loop
  -- dates from medicare tables
  if rec.msis_ind = 0 then
    sql_stmt := 'INSERT INTO /*+ APPEND*/ date_events'
              || ' SELECT /*+ PARALLEL('|| rec.table_name || ',12) */ ' 
              || '''' || rec.table_name || '''' || ' TABLE_NAME'
              || ', bene_id, null msis_id, null state_cd'
              || ', '|| '''' || rec.column_name || '''' || ' COL_DATE'
              || ',' || rec.column_name || ' DT'
              || ' from ' || rec.owner || '.' || rec.table_name
              || ' where ' || rec.column_name || ' is not null';
  else
  -- dates from medicaid tables
    sql_stmt := 'INSERT INTO /*+ APPEND*/ date_events'
              || ' SELECT /*+ PARALLEL('|| rec.table_name || ',12) */ ' 
              || '''' || rec.table_name || '''' || ' TABLE_NAME'
              || ', bene_id, msis_id, state_cd'
              || ', ' || '''' || rec.column_name || '''' || ' COL_DATE'
              || ',' || rec.column_name || ' DT'
              || ' from ' || rec.owner || '.' || rec.table_name
              || ' where ' || rec.column_name || ' is not null'; 
  end if;
  execute immediate sql_stmt;
  commit;
  end loop;
end;

/*~ 100sec/rec*/

select * from date_events;


