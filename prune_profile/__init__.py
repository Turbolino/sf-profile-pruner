# -*- coding: utf-8 -*-
"""Profile Pruner — a Salesforce Profile dry-run pruner.

Public package surface. The implementation is split into thematic modules:

- ``constants``  identity, version, namespace and the error-type mapping
- ``xmlio``      profile XML + package/manifest file I/O
- ``sfcli``      Salesforce CLI integration and sf/Node discovery
- ``board``      console logging and the live progress board
- ``state``      thread-safe shared pruning state
- ``engine``     parallel per-section pruning workers
- ``cli``        the command-line entry point (``main()``)
"""

from lxml import etree

from .board import Control, Logger, PrefetchStatus, Progress, StatusBoard
from .constants import (
    APP_NAME,
    AUTHOR_NOTE,
    BANNER,
    BLOCK_MAP,
    LISTABLE,
    M,
    MIN_API,
    NS,
    TERMINAL_STATUSES,
    __author__,
    __email__,
    __version__,
    version_ge,
)
from .engine import prefetch_dangling, process_section, worker
from .sfcli import (
    _candidate_dirs,
    _node_version,
    _run_sf,
    _sf_invocation,
    deploy,
    dry_run,
    find_node,
    find_sf,
    list_metadata,
    org_api_version,
    poll_deploy,
    report_deploy,
    start_deploy,
)
from .state import SharedState
from .xmlio import (
    apply_removals_batch,
    build_subprofile,
    candidates_for_block,
    dangling_members,
    extract_common_members,
    humanize_status,
    load_log_entries,
    prepare_work_copy,
    prunable_blocks,
    read_outcome,
    remove_block,
    remove_from_list,
    remove_temp_project,
    resolve_error,
    strip_blank_lines,
    write_package_xml,
    write_sfdx_project,
)

__all__ = [
    'APP_NAME', 'AUTHOR_NOTE', 'BANNER', 'BLOCK_MAP', 'LISTABLE', 'M',
    'MIN_API', 'NS', 'TERMINAL_STATUSES', '__version__', '__author__',
    '__email__', 'version_ge',
    'Control', 'Logger', 'PrefetchStatus', 'Progress', 'StatusBoard', 'SharedState',
    'prefetch_dangling', 'process_section', 'worker',
    '_candidate_dirs', '_node_version', '_run_sf', '_sf_invocation',
    'deploy', 'dry_run', 'find_node', 'find_sf', 'list_metadata',
    'org_api_version', 'poll_deploy', 'report_deploy', 'start_deploy',
    'apply_removals_batch', 'build_subprofile', 'candidates_for_block',
    'dangling_members', 'extract_common_members', 'humanize_status',
    'load_log_entries',
    'prepare_work_copy', 'prunable_blocks', 'read_outcome', 'remove_block',
    'remove_from_list', 'remove_temp_project', 'resolve_error',
    'strip_blank_lines', 'write_package_xml', 'write_sfdx_project', 'etree',
]
