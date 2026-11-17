// Describe selected SObjects via the Metadata/REST describe() call and print,
// as JSON on stdout, each field's `updateable` flag. Used by sf-profile-pruner
// to know which fields are inherently read-only (so a profile `editable=true`
// on them is dead metadata that must not be reported as a grant).
//
// This is a tiny, owned helper (AGPL-3.0). It reuses the Salesforce CLI's
// authentication (@salesforce/core) and does NOT depend on any third-party
// plugin. It is invoked by Python with NODE_PATH pointing at the sf global
// node_modules directory.
//
// Usage:  node describe_fields.cjs --username <username> --object <name> [...]
// Output: {"objects":[{"name":"...","fields":{"FieldApiName":true|false,...}}],"errors":[...]}
'use strict';

const { AuthInfo, Connection } = require('@salesforce/core');

function parseArgs(argv) {
  const args = { username: null, objects: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--username') {
      args.username = argv[++i];
    } else if (a === '--object') {
      args.objects.push(argv[++i]);
    }
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.username || args.objects.length === 0) {
    process.stderr.write(
      'usage: describe_fields.cjs --username <username> --object <name> [...]\n',
    );
    process.exit(2);
  }

  const authInfo = await AuthInfo.create({ username: args.username });
  const connection = await Connection.create({ authInfo });

  const out = { objects: [], errors: [] };
  for (const name of args.objects) {
    try {
      const d = await connection.describe(name);
      const fields = {};
      for (const f of d.fields || []) {
        fields[f.name] = Boolean(f.updateable);
      }
      out.objects.push({ name, fields });
    } catch (e) {
      out.errors.push({ name, error: (e && e.message) ? e.message : String(e) });
    }
  }

  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
  process.stderr.write('FATAL: ' + ((e && e.stack) ? e.stack : String(e)) + '\n');
  process.exit(1);
});
