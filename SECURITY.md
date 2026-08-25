# Security policy

Do not open a public issue containing API keys, Weixin credentials, pairing
codes, server addresses, database content, logs, or screenshots with personal
data.

For a suspected vulnerability, use GitHub's private security advisory feature
for this repository. Revoke exposed credentials before reporting them.

The application should be deployed with key-only SSH access, protected `.env`
files, per-user Hermes homes, and RSSHub/SQLite bound only to localhost.
