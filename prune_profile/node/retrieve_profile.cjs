// Retrieve a *complete* Salesforce Profile via the Metadata API CRUD
// readMetadata() call, and print the result as JSON on stdout.
//
// This is a tiny, owned helper for sf-profile-pruner (AGPL-3.0). It reuses the
// Salesforce CLI's authentication (@salesforce/core + jsforce) so it never
// touches the masked `sf org display` token, and it does NOT depend on any
// third-party plugin. It is invoked by Python with NODE_PATH pointing at the
// sf global node_modules directory.
//
// Usage:  node retrieve_profile.cjs --username <username> --profile <name> [...]
// Output: {"profiles":[{"fullName":"...","data":{...}}],"errors":[...]}
'use strict';

const { AuthInfo, Connection } = require('@salesforce/core');

function parseArgs(argv) {
  const args = { username: null, profiles: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--username') {
      args.username = argv[++i];
    } else if (a === '--profile') {
      args.profiles.push(argv[++i]);
    }
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.username || args.profiles.length === 0) {
    process.stderr.write(
      'usage: retrieve_profile.cjs --username <username> --profile <name> [--profile <name2> ...]\n',
    );
    process.exit(2);
  }

  const authInfo = await AuthInfo.create({ username: args.username });
  const connection = await Connection.create({ authInfo });

  const out = { profiles: [], errors: [] };
  for (const name of args.profiles) {
    try {
      // readMetadata returns a single object for one record, an array for many.
      const raw = await connection.metadata.read('Profile', [name]);
      const item = Array.isArray(raw) ? raw[0] : raw;
      if (!item || !item.fullName) {
        out.errors.push({ name, error: 'Profile not found in org or not returned by readMetadata()' });
        continue;
      }
      out.profiles.push({ fullName: item.fullName, data: item });
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
