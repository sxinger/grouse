from contextlib import contextmanager
from re import findall, match


def main(list_dir_argv, open_rd_argv, open_wr_cwd, pjoin, get_cli,
         fts_extension='.fts', oracle_create='oracle_create.sql',
         oracle_drop='oracle_drop.sql'):
    schema_name = get_cli()[2]

    base, files = list_dir_argv()
    table_to_ddl = dict()
    for fname in files:
        if fname.endswith(fts_extension):
            with open_rd_argv(pjoin(base, fname)) as fin:
                tname = file_to_table_name(fname)
                filedat = fin.read()
                ddl = oracle_ddl(schema_name, tname,
                                 fts_to_oracle_cols(fname, filedat))
                if tname in table_to_ddl.keys():
                    if table_to_ddl[tname] != ddl:
                        raise RuntimeError('Differing DDL for %s' % tname)
                table_to_ddl[tname] = ddl
    print len(table_to_ddl.keys())
    with open_wr_cwd(oracle_create) as fout:
        fout.write('\n\n'.join(table_to_ddl.values()))
    with open_wr_cwd(oracle_drop) as fout:
        for tname in table_to_ddl.keys():
            fout.write('drop table %s.%s;\n' % (schema_name, tname))


def oracle_ddl(schema_name, table_name, oracle_cols):
    return ('''create table %(schema_name)s.%(table_name)s (
    %(cols)s
    );''' % dict(schema_name=schema_name,
                 table_name=table_name,
                 cols=',\n'.join(oracle_cols)))


def file_to_table_name(fname, rev='j', skip='file'):
    '''
    >>> file_to_table_name('bcarrier_claims_j_res000050354_req005900_2011.fts')
    'bcarrier_claims'
    >>> file_to_table_name('medpar_all_file_res000050354_req005900_2011.fts')
    'medpar_all'
    >>> file_to_table_name('maxdata_ot_2011.fts')
    'maxdata_ot'
    '''
    new_parts = []
    for part in fname.split('.')[0].split('_'):
        if(part.startswith('res') or
           part == rev or
           part == skip or
           part.isdigit()):
            break
        new_parts.append(part)
    return '_'.join(new_parts)


def fts_to_oracle_cols(fname, filedat):
    fields = parse_fields(filedat)
    if findall('Type: Comma-Separated.*', filedat):
        return fts_csv_to_oracle_types(fields)
    elif findall('Format: Fixed Column ASCII', filedat):
        return fts_fixed_to_oracle_types(fields)
    else:
        raise NotImplementedError(fname)


def parse_fields(filedat):
    '''
    >>> def first_cols(rows):
    ...     return [c[:-1] for c in [r for r in rows]]
    >>> from pkg_resources import resource_string
    >>> first_cols(parse_fields(resource_string(__name__, 'sample_fixed')))
    ... # doctest: +NORMALIZE_WHITESPACE
    [['1', 'BENE_ID', 'BENE_ID', 'CHAR', '1', '15'],
    ['2', 'MEDPAR_ID', 'MEDPARID', 'CHAR', '16', '15'],
    ['3', 'MEDPAR_YR_NUM', 'MEDPAR_YR_NUM', 'CHAR', '31', '4']]

    >>> first_cols(parse_fields(resource_string(__name__, 'sample_csv')))
    ... # doctest: +NORMALIZE_WHITESPACE
    [['1', 'BENE_ID', 'CHAR', '$15.', '15'],
    ['2', 'MSIS_ID', 'CHAR', '$32.', '32'],
    ['3', 'STATE_CD', 'CHAR', '$2.', '2'], ['4', 'YR_NUM', 'NUM', '4.', '4']]
    '''
    widths = None
    rows = []
    for line in filedat.split('\n'):
        if widths and not line.strip():
            break
        elif widths:
            cols = []
            start = 0
            for width in widths:
                cols.append(line[start:start + width].strip())
                start += width + 1
            rows.append(cols)
        else:
            m = match('^-+\s+', line)
            if m:
                widths = [len(c.strip()) for c in line.split(' ') if c.strip()]
    return rows


def fts_csv_to_oracle_types(fields, name_idx=1, type_idx=2, width_idx=4):
    return [oracle_type(col[name_idx], col[type_idx], col[width_idx])
            for col in fields]


def fts_fixed_to_oracle_types(fields, name_idx=2, type_idx=3,
                              start_idx=4, width_idx=5):
    return [oracle_type(col[name_idx], col[type_idx], col[width_idx])
            for col in fields]


def oracle_type(name, dtype, width, xlate=dict([
        ('CHAR', 'VARCHAR2(%(width)s)'),
        ('NUM', 'NUMBER(%(prsc)s)'),
        ('DATE', 'DATE')])):
    return '%s %s' % (name,  xlate[dtype] %
                      dict(width=width,
                           prsc=('%s,%s' % tuple(width.split('.'))
                                 if '.' in width else width)))

if __name__ == '__main__':
    def _tcb():
        from os import listdir, getcwd
        from os.path import join as pjoin, abspath
        from sys import argv

        def get_input_path():
            return argv[1]

        def get_cli():
            return argv

        def list_dir_argv():
            return get_input_path(), listdir(get_input_path())

        @contextmanager
        def open_rd_argv(path):
            if get_input_path() not in path:
                raise RuntimeError('%s not in %s' % (get_input_path(), path))
            with open(path, 'rb') as fin:
                yield fin

        @contextmanager
        def open_wr_cwd(path):
            cwd = abspath(getcwd())
            if cwd not in abspath(path):
                raise RuntimeError('%s not in %s' % (cwd, path))
            with open(path, 'wb') as fin:
                yield fin

        main(list_dir_argv, open_rd_argv, open_wr_cwd, pjoin, get_cli)

    _tcb()
