import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


EXCLUDED_DIRS = {
    'hms',
    'tundra',
    'umass',
    'mit',
}


INACTIVE = 'Archived/Deleted'
SIZE_THRESHOLD_GB = 500


OUTPUT_COLUMNS = [
    'Project ID',
    'Workspace ID',
    'Workspace Date',
    'Project Owner',
    'Microscope',
    'Project Title',
    'Workspace Title',
    'Raw Data Path',
    'Current Raw Data Size',
    'Raw Data Archived/Deleted?',
    'Project Path',
    'Current Project Size',
    'Project Archived/Deleted?',
    'Comments',
]


DUC_LINE = re.compile(
    r'^\s*'
    r'(?P<size>\d+(?:\.\d+)?[KMGTPE]?) '
    r'(?P<prefix>.*?)'
    r'(?P<connector>\|-\s+|`\+-\s+|`-\s+)'
    r'(?P<name>.+?)\s*$'
)


SIZE_VALUE = re.compile(
    r'^(?P<number>\d+(?:\.\d+)?)\s*'
    r'(?P<unit>[KMGTPE]?)'
    r'(?:I?B)?$',
    re.IGNORECASE,
)


WORKSPACE_NUMBER = re.compile(r'W(\d+)', re.IGNORECASE)




def normalize_path(path):
    return os.path.normpath(os.path.abspath(os.fspath(path)))




def owner_from_project_path(project_path):
    parts = project_path.split('/')
    return parts[4] if len(parts) > 4 else ''




def project_only_row(project_path):
    return {
        'Project Owner': owner_from_project_path(project_path),
        'Project Path': project_path,
        'Project Archived/Deleted?': INACTIVE,
        'Comments': '',
    }




def is_true(value):
    return str(value).strip().lower() == 'true'




def microscope_from_voltage(value):
    if value is None:
        return None


    value = str(value).strip()
    return {
        '100': 'Tundra',
        '200': 'Arctica',
        '300': 'Krios',
    }.get(value, value)




def workspace_microscope(workspace):
    for parameter in workspace.get('parameters', []):
        if parameter.get('title') != 'Microscope/Camera Parameter':
            continue


        for item in parameter.get('contains', []):
            if item.get('title') == 'Accelerating voltage (kV)':
                return microscope_from_voltage(item.get('value'))


    return None




def workspace_date(workspace):
    created_at = workspace.get('created_at') or {}
    if not isinstance(created_at, dict):
        return None


    value = created_at.get('$date')


    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(
                value / 1000.0,
                tz=timezone.utc,
            ).strftime('%Y-%m-%d')


        if isinstance(value, str) and value:
            return datetime.fromisoformat(
                value.replace('Z', '+00:00')
            ).strftime('%Y-%m-%d')
    except (ValueError, OverflowError, OSError):
        pass


    return None




def true_raw_data_dir(symlinks_dir):
    """
    Preserve the original policy: use the parent of the first symlink's
    target. Return symlinks_dir if there are no usable symlinks.
    """
    directory = Path(symlinks_dir)


    try:
        if not directory.is_dir():
            return os.fspath(directory)


        symlinks = sorted(
            entry for entry in directory.iterdir()
            if entry.is_symlink()
        )
        if not symlinks:
            return os.fspath(directory)


        link = symlinks[0]
        target = Path(os.readlink(link))


        if not target.is_absolute():
            target = link.parent / target


        return os.fspath(Path(os.path.normpath(str(target))).parent)
    except OSError:
        return os.fspath(directory)




def imported_paths_by_workspace(project_path):
    """
    Read imported-movie jobs at most once per project.


    Return {workspace_uid: (raw_path, microscope)}.
    """
    result = {}


    try:
        with os.scandir(project_path) as entries:
            job_dirs = sorted(
                entry.path for entry in entries
                if entry.is_dir()
            )
    except OSError:
        return result


    for job_dir in job_dirs:
        imported_dir = os.path.join(job_dir, 'imported')
        job_json = os.path.join(job_dir, 'job.json')


        if not os.path.isdir(imported_dir):
            continue
        if not os.path.isfile(job_json):
            continue


        try:
            with open(job_json, 'r') as file:
                job = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue


        params = job.get('params_spec') or {}
        blob_paths = params.get('blob_paths') or {}
        if not isinstance(blob_paths, dict):
            continue


        raw_path = blob_paths.get('value')
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue


        accel_kv = params.get('accel_kv') or {}
        voltage = (
            accel_kv.get('value')
            if isinstance(accel_kv, dict)
            else None
        )
        microscope = microscope_from_voltage(voltage)


        for uid in job.get('workspace_uids') or []:
            result.setdefault(uid, (raw_path, microscope))


    return result




def clean_raw_path(raw_path):
    if not isinstance(raw_path, str):
        return ''


    raw_path = raw_path.strip()
    if raw_path in ('', 'null', 'N/A', '/', '\\'):
        return raw_path


    # If the path points inside a symlinks directory, inspect that
    # directory—not its parent.
    if '/symlinks/' in raw_path or raw_path.endswith('/symlinks'):
        symlinks_dir = raw_path.split('/symlinks', 1)[0] + '/symlinks'
        raw_path = true_raw_data_dir(symlinks_dir)


    for marker in ('/Images-Disc1', '/Raw_data'):
        position = raw_path.find(marker)
        if position != -1:
            raw_path = raw_path[:position]


    return normalize_path(raw_path)




def raw_directory_exists(raw_path, cache):
    if raw_path in ('', '/', '\\', 'null', 'N/A'):
        return False


    key = normalize_path(raw_path)


    if key not in cache:
        cache[key] = os.path.isdir(key)


    return cache[key]




def extract_project(project_dir, start_day, end_day, rows, exists_cache):
    project_path = normalize_path(project_dir)
    project_json = os.path.join(project_path, 'project.json')
    workspaces_json = os.path.join(project_path, 'workspaces.json')


    if not (
        os.path.isfile(project_json)
        and os.path.isfile(workspaces_json)
    ):
        try:
            entries = os.listdir(project_path)
        except OSError:
            entries = []


        # Correctly check for a single .txt file; do not use *'.txt'
        # inside os.path.join().
        if len(entries) == 1 and entries[0].endswith('.txt'):
            rows.append(project_only_row(project_path))
        else:
            print(f'No folder information for {project_path}')
        return

    for metadata_path in (project_json, workspaces_json):
        try:
            with open(metadata_path, 'r') as file:
                parsed = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            print(f'Skipping unreadable metadata file {metadata_path}: {error}')
            return

        if metadata_path == project_json:
            project_data = parsed
        else:
            workspaces_data = parsed

    if not isinstance(project_data, dict):
        print(f'Skipping unexpected project format: {project_path}')
        return

    if not isinstance(workspaces_data, list):
        print(f'Skipping unexpected workspaces format: {project_path}')
        return

    project = project_data
    workspaces = workspaces_data

    project_status = (
        INACTIVE
        if is_true(project.get('deleted')) or is_true(project.get('archived'))
        else 'Active'
    )


    imported_paths = None  # Populate only if a workspace needs it.


    for workspace in workspaces:
        date = workspace_date(workspace)


        if date is None:
            print(
                f"Skipping workspace {workspace.get('uid', '')} "
                f"in {project_path}: unrecognized date"
            )
            continue


        # Preserve the original exclusive date boundaries.
        if not (start_day < date < end_day):
            continue


        uid = workspace.get('uid', '')
        microscope = workspace_microscope(workspace)


        exposure_groups = workspace.get('exposure_groups') or []
        first_group = exposure_groups[0] if exposure_groups else {}
        raw_path = (
            first_group.get('file_engine_watch_path_abs', '')
            if isinstance(first_group, dict)
            else ''
        )


        if raw_path in (None, '', 'null', 'N/A'):
            if imported_paths is None:
                imported_paths = imported_paths_by_workspace(project_path)


            imported = imported_paths.get(uid)
            if imported:
                raw_path, imported_microscope = imported
                if imported_microscope is not None:
                    microscope = imported_microscope


        # Preserve the original behavior for workspaces with no
        # identifiable raw path. "/" is retained and handled below.
        if raw_path in (None, '', 'null', 'N/A'):
            continue


        raw_path = clean_raw_path(raw_path)
        exists = raw_directory_exists(raw_path, exists_cache)


        rows.append({
            'Project ID': project.get('uid', ''),
            'Workspace ID': uid,
            'Workspace Date': date,
            'Project Owner': owner_from_project_path(project_path),
            'Microscope': microscope,
            'Project Title': project.get('title', ''),
            'Workspace Title': workspace.get('title', ''),
            'Raw Data Path': raw_path,
            'Current Raw Data Size': '',
            'Raw Data Archived/Deleted?': '',
            'Project Path': project_path,
            'Current Project Size': '',
            'Project Archived/Deleted?': project_status,
            'Comments': '',


            # Internal field, removed from the CSV.
            '_Raw Directory Exists': exists,
        })




def requested_duc_sizes(duc_file, requested_paths, project_paths):
    """
    Make one pass through the duc tree. Retain sizes only for paths we
    requested. A match requires the complete printed tree path to match
    the end of the requested absolute path.
    """
    targets = {
        normalize_path(path)
        for path in requested_paths
        if isinstance(path, str) and path not in ('', '/', '\\')
    }


    targets_by_name = {}


    for target in targets:
        name = os.path.basename(target)
        targets_by_name.setdefault(name, []).append(
            (target, Path(target).parts[1:])
        )


    found = {}
    stack = []


    job_targets_by_name = {}
    for path in project_paths:
        target = normalize_path(path)
        name = os.path.basename(target)
        job_targets_by_name.setdefault(name, []).append(
            (target, Path(target).parts[1:])
        )


    project_jobs = {}

    with open(duc_file, 'r') as file:
        for line_number, line in enumerate(file, 1):
            match = DUC_LINE.match(line)


            if match is None:
                continue


            name = match.group('name')
            if not name.endswith('/'):
                continue


            prefix = match.group('prefix')
            connector = match.group('connector')


            # "|-" and "`-" use one extra prefix character compared
            # with "`+-" in this tree format.
            padding = 1 if connector.startswith(('|-', '`-')) else 0
            adjusted = len(prefix) - padding


            if adjusted < 0 or adjusted % 4:
                raise ValueError(
                    f'Unexpected duc indentation at line {line_number}: '
                    f'{line.rstrip()}'
                )


            depth = adjusted // 4
            stack = stack[:depth]


            if len(stack) != depth:
                raise ValueError(
                    f'Missing tree parent at line {line_number}: '
                    f'{line.rstrip()}'
                )


            name = name.rstrip('/')
            stack.append(name)

            # A job must be an immediate child of a requested project.
            if re.fullmatch(r'[JS]\d+', name, re.IGNORECASE):
                parent_parts = tuple(stack[:-1])
                parent_name = stack[-2] if len(stack) > 1 else None


                for target, target_parts in job_targets_by_name.get(
                    parent_name, ()
                ):
                    if (
                        len(target_parts) >= len(parent_parts)
                        and target_parts[-len(parent_parts):] == parent_parts
                    ):
                        job_gb, _ = parse_size(match.group('size'))
                        if job_gb is not None and job_gb > 500:
                            project_jobs.setdefault(target, []).append(
                                (name, job_gb)
                            )


            for target, target_parts in targets_by_name.get(name, ()):
                tree_parts = tuple(stack)


                if (
                    len(target_parts) >= len(tree_parts)
                    and target_parts[-len(tree_parts):] == tree_parts
                ):
                    if target in found:
                        raise ValueError(
                            f'Multiple duc tree entries match {target}; '
                            'cannot safely choose a size.'
                        )


                    found[target] = match.group('size')


    return found, project_jobs




def parse_size(value):
    """
    Return (size_in_GB, original_unit).


    Uses decimal conversion: 1000 G = 1 T, as requested.
    Returns (None, None) when the size is unknown.
    """
    if value is None or pd.isna(value):
        return None, None


    text = str(value).strip()
    if text.upper() in ('', 'NAN', 'UNKNOWN', '!!UNKNOWN!!', 'N/A'):
        return None, None


    match = SIZE_VALUE.fullmatch(text)
    if match is None:
        raise ValueError(f'Unrecognized size: {value!r}')


    number = float(match.group('number'))
    unit = match.group('unit').upper()


    in_gb = {
        '': number / (1000 ** 3),  # bytes
        'K': number / (1000 ** 2),
        'M': number / 1000,
        'G': number,
        'T': number * 1000,
        'P': number * (1000 ** 2),
        'E': number * (1000 ** 3),
    }[unit]


    return in_gb, unit


def size_in_tb_string(value):
    in_gb, _unit = parse_size(value)
    if in_gb is None:
        return ''

    formatted = f'{in_gb / 1000:.1f}T'
    return '' if formatted == '0.0T' else formatted


def classify_existing_raw_directory(path, cache):
    """
    Preserve the earlier small-directory archive test for directories
    whose size is NOT in M. Cache results for shared raw paths.
    """
    if path in cache:
        return cache[path]


    file_count = 0
    txt_count = 0


    with os.scandir(path) as entries:
        for entry in entries:
            if entry.name.startswith('.'):
                continue


            if entry.is_dir():
                cache[path] = 'Active'
                return 'Active'


            if entry.is_file():
                file_count += 1
                if entry.name.endswith('.txt'):
                    txt_count += 1


            if file_count > 10:
                cache[path] = 'Active'
                return 'Active'


    if file_count == 0 or (file_count < 10 and txt_count < 3):
        status = INACTIVE
    else:
        status = 'Active'


    cache[path] = status
    return status




def add_sizes_and_statuses(rows, duc_sizes, project_jobs):
    raw_status_cache = {}


    for row in rows:
        project_path = row.get('Project Path')
        raw_path = row.get('Raw Data Path')
        raw_exists = row.get('_Raw Directory Exists', False)


        project_size = (
            duc_sizes.get(normalize_path(project_path), '')
            if project_path else ''
        )
        raw_size = (
            duc_sizes.get(normalize_path(raw_path), '')
            if raw_exists else ''
        )

        row['Current Project Size'] = project_size
        row['Current Raw Data Size'] = raw_size

        project_gb, project_unit = parse_size(project_size)

        if project_gb is not None and project_gb > 4000:
            jobs = sorted(
                project_jobs.get(normalize_path(project_path), ()),
                key=lambda job: (-job[1], job[0]),
            )
            if jobs:
                row['Comments'] = 'Largest Jobs: ' + ', '.join(
                    f'{name} [{size_gb / 1000:.1f}T]'
                    for name, size_gb in jobs
                )

        raw_gb, raw_unit = parse_size(raw_size)

        # Match the one-decimal-TB formatting used in the CSV.
        project_displays_zero = (
            project_gb is not None
            and f'{project_gb / 1000:.1f}T' == '0.0T'
        )
        raw_displays_zero = (
            raw_gb is not None
            and f'{raw_gb / 1000:.1f}T' == '0.0T'
        )

        if project_unit == 'M' or project_displays_zero:
            row['Project Archived/Deleted?'] = INACTIVE

        if not raw_exists or raw_unit == 'M' or raw_displays_zero:
            row['Raw Data Archived/Deleted?'] = INACTIVE
        else:
            row['Raw Data Archived/Deleted?'] = (
                classify_existing_raw_directory(
                    raw_path,
                    raw_status_cache,
                )
            )


def should_keep_row(row):
    project_gb, _ = parse_size(row['Current Project Size'])
    raw_gb, _ = parse_size(row['Current Raw Data Size'])


    # Preserve the agreed rule for projects with no known sizes.
    if project_gb is None and raw_gb is None:
        return True


    return (project_gb or 0) + (raw_gb or 0) > SIZE_THRESHOLD_GB




def workspace_number(value):
    match = WORKSPACE_NUMBER.search(str(value))
    return int(match.group(1)) if match else float('inf')




def select_project_rows(df):
    """
    Qualify a project if any of its rows meets the size test.
    Then keep all its Active workspaces, or its lowest-numbered
    workspace if none are Active.
    """
    selected = []


    for _, group in df.groupby(
        'Project Path',
        dropna=False,
        sort=False,
    ):
        if not any(
            should_keep_row(row)
            for _, row in group.iterrows()
        ):
            continue


        active = group[
            group['Raw Data Archived/Deleted?'] == 'Active'
        ]


        if not active.empty:
            selected.extend(active.index)
        else:
            chosen = min(
                group.index,
                key=lambda index: (
                    workspace_number(
                        group.at[index, 'Workspace ID']
                    ),
                    index,
                ),
            )
            selected.append(chosen)


    return df.loc[selected].copy()




def main():
    if len(sys.argv) != 5:
        print(
            'Usage: script.py <directory> <duc_output_location> '
            '<start_day YYYYMMDD> <end_day YYYYMMDD>'
        )
        sys.exit(1)


    directory = sys.argv[1]
    output_dir = sys.argv[2]
    duc_file = os.path.join(output_dir, 'duc_output.txt')


    start_day = datetime.strptime(
        sys.argv[3], '%Y%m%d'
    ).strftime('%Y-%m-%d')


    end_day = datetime.strptime(
        sys.argv[4], '%Y%m%d'
    ).strftime('%Y-%m-%d')


    rows = []
    exists_cache = {}


    for root, dirs, _files in os.walk(directory):
        dirs[:] = [
            name for name in dirs
            if name not in EXCLUDED_DIRS
        ]


        for name in dirs[:]:
            if name.startswith('CS-'):
                project_path = os.path.join(root, name)
                print(f'Processing {project_path}')


                extract_project(
                    project_path,
                    start_day,
                    end_day,
                    rows,
                    exists_cache,
                )


                # Do not descend through all the project's job folders.
                dirs.remove(name)


    project_paths = {
        row['Project Path']
        for row in rows
        if row.get('Project Path')
    }


    requested_paths = set(project_paths)
    requested_paths.update(
        row['Raw Data Path']
        for row in rows
        if (
            row.get('_Raw Directory Exists')
            and row.get('Raw Data Path')
        )
    )


    duc_sizes, project_jobs = requested_duc_sizes(
        duc_file, requested_paths, project_paths
    )
    add_sizes_and_statuses(rows, duc_sizes, project_jobs)


    df = pd.DataFrame(rows)


    if df.empty:
        df = pd.DataFrame(columns=OUTPUT_COLUMNS)
    else:
        df.reset_index(drop=True, inplace=True)
        df = select_project_rows(df)


        # Make the choice among identical path pairs deterministic:
        # prefer the lowest-numbered workspace.
        df['_Workspace Number'] = df['Workspace ID'].map(
            workspace_number
        )
        df.sort_values(
            by=[
                'Project Path',
                'Raw Data Path',
                '_Workspace Number',
                'Workspace Date',
            ],
            kind='stable',
            na_position='last',
            inplace=True,
        )
        df.drop_duplicates(
            subset=['Project Path', 'Raw Data Path'],
            keep='first',
            inplace=True,
        )


        # Convert only AFTER filtering and deciding which unit was M.
        for column in (
            'Current Raw Data Size',
            'Current Project Size',
        ):
            df[column] = df[column].map(size_in_tb_string)


        df.sort_values(
            by=['Project Owner', 'Workspace Date'],
            kind='stable',
            na_position='last',
            inplace=True,
        )


        # Also removes internal cache/selection fields.
        df = df.reindex(columns=OUTPUT_COLUMNS)


    output_file = os.path.join(
        output_dir,
        f'scrape_out_{start_day}_{end_day}.csv',
    )
    df.to_csv(output_file, index=False)
    print(f'Wrote {output_file}')




if __name__ == '__main__':
    main()
