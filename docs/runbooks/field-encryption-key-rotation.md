# Field-encryption key rotation gate

`FIELD_ENCRYPTION_KEY` currently uses one Fernet key for both encryption and
decryption, and `core.fields._fernet` caches it for the process lifetime.

Changing that setting immediately makes every value written under the former
key unreadable. The current application has no multi-key decrypt window and no
reviewed, resumable tenant-aware re-encryption command. Therefore key rotation
is **not supported in place in this release**. This is an explicit operational
release requirement, not a command to improvise during an incident.

Until the rotation feature is implemented:

1. Store the active key in a versioned production secrets manager with tightly
   audited access and an offline recovery copy.
2. Verify the same key is injected into every application and migration
   workload. Never log or place it in shell history.
3. Restart every web, ASGI, worker, beat, and migration process after any key
   configuration correction; a hot setting change does not clear the cache.
4. Never rotate the key in the same release as a field/data migration.
5. Treat loss of the only key as unrecoverable data loss and disclosure of the
   key as a security incident requiring a maintenance window.

A future rotation implementation must pass these gates before an old key is
retired:

- encrypt with the new primary key while decrypting with the new and previous
  keys;
- re-encrypt every encrypted field in the public schema and every tenant schema
  in bounded, resumable transactions;
- authenticate every non-empty token with the new key alone and publish only
  row counts, never values;
- prove mixed old/new data remains readable during a rollback window;
- prove corrupt ciphertext stops the rotation without deleting or replacing
  the source value;
- restart all processes, remove the old decrypt key, and rerun ciphertext and
  application smoke tests.
