# -*- coding: utf-8 -*-
"""Package-level constants for Profile Pruner.

Identity/version, the ASCII banner, the Salesforce metadata namespace and the
error-type -> (block, child, mode) mapping that drives the pruning. Kept free
of I/O so every other module can import it without side effects.
"""

__version__ = '1.10.6'
__author__ = "Lorenzo D'Aleo"
__email__ = 'turbolino.dev@proton.me'
AUTHOR_NOTE = "Made with <3 by %s (%s)" % (__author__, __email__)

APP_NAME = 'Profile Pruner'

BANNER = r'''
  _____   ______  _____  _______ _____        _______       _____   ______ _     _ __   _ _______  ______
 |_____] |_____/ |     | |______   |   |      |______      |_____] |_____/ |     | | \  | |______ |_____/
 |       |    \_ |_____| |       __|__ |_____ |______      |       |    \_ |_____| |  \_| |______ |    \_
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
    'ApexPage':           [('pageAccesses', 'apexPage', 'exact')],
    'CustomApplication':  [('applicationVisibilities', 'application', 'exact')],
    'Flow':               [('flowAccesses', 'flow', 'exact')],
    'FlowDefinition':     [('flowAccesses', 'flow', 'exact')],
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
}
