# 2026-05-16 — TLS cert lifetime fix (Apple 398-day cap)

## What was wrong

`scripts/gen_ssl_cert.py` minted leaf certs with `CERT_VALIDITY_YEARS = 10` — the same constant the script used for the CA. Since iOS 14, Apple's TLS stack rejects server certs with a validity period > 398 days **even when the issuing CA is fully trusted** in the iOS profile keychain.

Symptom on iPhone:
- Safari shows "Not Secure" on the HTTPS URL despite the trust profile being installed and "Enable Full Trust" toggled on for "Launcher Local CA".
- PWA *Add to Home Screen* fetches a blank icon (the `apple-touch-icon` fetch fails over the still-untrusted TLS).
- No useful error surface — Safari silently downgrades trust.

Not noticed on the PC (Chrome/Edge against the user trust store don't enforce the 398-day cap — only Apple's TLS stack does). The same script is copy-pasted into the sister projects `photo-ocr` and `voice-transcriber`; same latent bug, tracked at photo-ocr#3 and voice-transcriber#10.

## What was done

### `scripts/gen_ssl_cert.py`

- Split the validity constant into `CA_VALIDITY_DAYS = 365 * 10` and `LEAF_VALIDITY_DAYS = 395`. The 1-day backdate (`not_valid_before = now - 1 day`) makes the leaf's *lifetime* `LEAF_VALIDITY_DAYS + 1 = 396 days`, giving a 2-day margin under Apple's 398-day cap.
- Added `_load_or_build_ca()` which reuses an existing `ca.pem` + `ca.key` if present (logs how many days the CA has left). Leaf rotations no longer force iPhone re-trust.
- Added `--force-new-ca` CLI flag for when a fresh CA *is* wanted (e.g. CA key compromise). Documented that re-running with it forces a re-install of the trust profile on every device.

### `README.md`

- Expanded "Phone install (PWA)" with the full one-time iPhone trust setup — including the two easy-to-miss steps: toggling "Enable Full Trust" in *Certificate Trust Settings*, and force-quitting Safari to clear its negative-trust cache.
- New "TLS cert: regenerate every ~13 months" section covering routine rotation, `--force-new-ca`, and a troubleshooting table for the "Not Secure" symptom.

## Validation

1. Patched the script and ran `& .\.venv\Scripts\python.exe scripts/gen_ssl_cert.py --skip-install`. Log line `♻️  reusing existing CA from … (expires in 3649 days)` confirmed CA reuse.
2. `openssl x509 -in webapp/certificates/cert.pem -noout -dates` → `notBefore=May 15 2026 GMT`, `notAfter=Jun 15 2027 GMT` (396-day lifetime, within Apple's cap).
3. `openssl x509 -in webapp/certificates/ca.pem -noout -fingerprint -sha256` → `EC:FD:F5:15:92:A2:65:07:…` matched the fingerprint embedded in the iPhone's installed profile (verified by extracting the `PayloadContent` from the live `launcher-ca.mobileconfig`).
4. Restarted the webapp (`Stop-Process` the listener on `:8445` → spawn fresh uvicorn with the new cert; session-host on `:8446` left running so live PTY sessions survived).
5. On iPhone: force-quit Safari, reopened `https://tower.tail1121fd.ts.net:8445?token=…` — lock icon solid, no "Not Secure". Re-added the PWA to home screen with the proper rocket icon. Opened a PTY session, tapped the new 📋 button, pasted a multi-line snippet from the voice-transcriber app — text inserted into `claude` cleanly.

## Files modified

- `scripts/gen_ssl_cert.py`
- `README.md`
- `docs/2026-05-16-tls-cert-rotation.md` (this file)

## Related

- Issue #21 (app-launcher) — this fix
- Issue #16 (app-launcher) — paste button (Step 0); validated end-to-end alongside this cert work
- Issue #3 (photo-ocr) — same latent bug in sister project
- Issue #10 (voice-transcriber) — same latent bug in sister project
