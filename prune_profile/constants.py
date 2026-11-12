# -*- coding: utf-8 -*-
"""Package-level constants for Profile Pruner.

Identity/version, the ASCII banner, the Salesforce metadata namespace and the
error-type -> (block, child, mode) mapping that drives the pruning. Kept free
of I/O so every other module can import it without side effects.
"""

__version__ = '1.12.3'
__author__ = "Lorenzo D'Aleo"
__email__ = 'turbolino.dev@proton.me'
AUTHOR_NOTE = "Made with <3 by %s (%s)" % (__author__, __email__)

APP_NAME = 'Profile Pruner'

BANNER = '''
▄█████ ██████   █████▄ ▄▄▄▄   ▄▄▄  ▄▄▄▄▄ ▄▄ ▄▄    ▄▄▄▄▄   █████▄ ▄▄▄▄  ▄▄ ▄▄ ▄▄  ▄▄ ▄▄▄▄▄ ▄▄▄▄
▀▀▀▄▄▄ ██▄▄     ██▄▄█▀ ██▄█▄ ██▀██ ██▄▄  ██ ██    ██▄▄    ██▄▄█▀ ██▄█▄ ██ ██ ███▄██ ██▄▄  ██▄█▄
█████▀ ██       ██     ██ ██ ▀███▀ ██    ██ ██▄▄▄ ██▄▄▄   ██     ██ ██ ▀███▀ ██ ▀██ ██▄▄▄ ██ ██
'''

NS = 'http://soap.sforce.com/2006/04/metadata'
M = '{%s}' % NS

TERMINAL_STATUSES = {'Succeeded', 'SucceededPartial', 'Failed', 'Canceled'}

# Error-type -> list of (block, child, mode)
# mode: 'exact' | 'prefix' (X__c.*) | 'suffix' (-X)
BLOCK_MAP = {
    'CustomField':        [('fieldPermissions', 'field', 'exact')],
    'StandardField':      [('fieldPermissions', 'field', 'exact')],
    'CustomTab':          [
        ('tabVisibilities', 'tab', 'exact'),
        ('tabVisibilities', 'tab', 'prefix'),
        ('tabVisibilities', 'tab', 'suffix'),
    ],
    'ApexClass':          [('classAccesses', 'apexClass', 'exact')],
    'ApexPage':           [
        ('pageAccesses', 'apexPage', 'exact'),
        # Visualforce-type login flows also reference an ApexPage.
        ('loginFlows', 'visualForcePage', 'exact'),
    ],
    'CustomApplication':  [('applicationVisibilities', 'application', 'exact')],
    'Flow':               [
        ('flowAccesses', 'flow', 'exact'),
        # VisualWorkflow-type login flows reference the flow by name too.
        ('loginFlows', 'flow', 'exact'),
    ],
    'FlowDefinition':     [
        ('flowAccesses', 'flow', 'exact'),
        # "no FlowDefinition named X found" can come from a login flow whose
        # flow doesn't exist in the target org.
        ('loginFlows', 'flow', 'exact'),
    ],
    'Layout':             [('layoutAssignments', 'layout', 'exact')],
    'RecordType':         [
        ('recordTypeVisibilities', 'recordType', 'exact'),
        ('layoutAssignments', 'recordType', 'exact'),
    ],
    'CustomPermission':   [('customPermissions', 'name', 'exact')],
    'CustomMetadataType': [('customMetadataTypeAccesses', 'name', 'exact')],
    'ExternalDataSource': [('externalDataSourceAccesses', 'externalDataSource', 'exact')],
    'CustomObject':       [
        ('objectPermissions', 'object', 'exact'),
        ('fieldPermissions', 'field', 'prefix'),
        ('customSettingAccesses', 'name', 'exact'),
    ],
    'StandardObject':     [('objectPermissions', 'object', 'exact')],
    'ServicePresenceStatus': [('servicePresenceStatusAccesses', 'servicePresenceStatus', 'exact')],
    'DataCategoryGroup':  [('categoryGroupVisibilities', 'dataCategoryGroup', 'exact')],
    # Newer Profile sections (introduced in API 63.0 / 66.0). The child element
    # name is not yet pinned down by the WSDL, so both variants are listed; a
    # non-matching name is simply never hit.
    'AgentDefinition':    [
        ('agentAccesses', 'agentName', 'exact'),
        ('agentAccesses', 'agent', 'exact'),
    ],
    'GenComputingSummaryDefinition': [
        ('genComputingSummaryDefAccesses', 'name', 'exact'),
        ('genComputingSummaryDefAccesses', 'genComputingSummaryDef', 'exact'),
    ],
}

# ---------------------------------------------------------------------------
# Pre-fetch: enumerate existing org metadata via `sf org list metadata` and
# prune dangling references BEFORE the dry-run loop. Only types whose
# listMetadata enumeration is complete are here.
#
# CustomField / CustomObject / StandardObject are deliberately absent:
# listMetadata does NOT return standard fields/objects (only custom ones), so
# enumerating them would prune every valid standard reference. Those stay
# handled by the per-section dry-run, which is authoritative.
# ---------------------------------------------------------------------------

# metadata-type name (for `sf org list metadata`) -> [(block, child)]
LISTABLE = {
    'ApexClass':          [('classAccesses', 'apexClass')],
    'ApexPage':           [('pageAccesses', 'apexPage'),
                           ('loginFlows', 'visualForcePage')],
    'CustomApplication':  [('applicationVisibilities', 'application')],
    'CustomTab':          [('tabVisibilities', 'tab')],
    'CustomPermission':   [('customPermissions', 'name')],
    'CustomMetadataType': [('customMetadataTypeAccesses', 'name')],
    'ExternalDataSource': [('externalDataSourceAccesses', 'externalDataSource')],
    'Flow':               [('flowAccesses', 'flow'),
                           ('loginFlows', 'flow')],
    'Layout':             [('layoutAssignments', 'layout')],
    'RecordType':         [('recordTypeVisibilities', 'recordType'),
                           ('layoutAssignments', 'recordType')],
    'ServicePresenceStatus': [('servicePresenceStatusAccesses', 'servicePresenceStatus')],
    'DataCategoryGroup':  [('categoryGroupVisibilities', 'dataCategoryGroup')],
    'AgentDefinition':    [('agentAccesses', 'agentName'),
                           ('agentAccesses', 'agent')],
    'GenComputingSummaryDefinition': [('genComputingSummaryDefAccesses', 'name'),
                                      ('genComputingSummaryDefAccesses', 'genComputingSummaryDef')],
}

# Minimum API version that introduced the corresponding Profile section; the
# type is not fetched below it.
MIN_API = {
    'AgentDefinition': '63.0',
    'GenComputingSummaryDefinition': '66.0',
}


def version_ge(a, b):
    """Compare dotted numeric versions: True if a >= b (missing parts = 0)."""
    def _tup(v):
        out = []
        for part in str(v).split('.'):
            digits = ''.join(ch for ch in part if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return out
    ta, tb = _tup(a), _tup(b)
    while len(ta) < len(tb):
        ta.append(0)
    while len(tb) < len(ta):
        tb.append(0)
    return ta >= tb

