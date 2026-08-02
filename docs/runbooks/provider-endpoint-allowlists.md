# Provider endpoint allowlists

Outbound integrations that carry credentials must use an explicit HTTPS URL
and a separate exact-hostname allowlist. Production fails during startup when
an enabled provider has an empty allowlist, a host mismatch, credentials in the
URL, a wildcard, an IP literal, a non-default port, a query, or a fragment.

## Configuration

1. Obtain the provider's contracted HTTPS endpoint from an approved source.
2. Put the full base URL in the provider URL setting. For Soliq this is
   `SOLIQ_API_URL`.
3. Put only the URL's exact TLS hostname in the paired allowlist. For Soliq this
   is `SOLIQ_API_ALLOWED_HOSTS`. Do not include `https://`, a path, a wildcard,
   or an IP address.
4. Review URL and allowlist changes together. A provider host migration is a
   configuration change requiring the same review as a credential rotation.
5. Run `python manage.py check --deploy --fail-level WARNING` inside the exact
   candidate image with the proposed production environment before deployment.

The repository's safe, non-routable validation pair is:

```dotenv
SOLIQ_API_URL=https://soliq.example.invalid
SOLIQ_API_ALLOWED_HOSTS=soliq.example.invalid
```

Replace both values with the contracted endpoint before setting
`FISCALIZATION_ENABLED=True`. An empty or mismatched allowlist must never be
bypassed merely to make startup succeed.
