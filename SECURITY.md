# Security Policy

## Supported versions

Only the latest commit on `main` is supported.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/emreyigit-tasdelen/Carlsbergsky/security/advisories/new)
instead. You will get an answer as soon as possible.

Vulnerabilities in Kaspersky Security Center itself or in Kaspersky's
`ksc_fast_install.py` should be reported to Kaspersky, not here.

## Known limitations

Carlsbergsky accepts unknown SSH host keys (`paramiko.AutoAddPolicy`) and
supports password authentication only. Use it on networks you trust. See
[Security and limitations](README.md#security-and-limitations).

---

<sub>© 2026 Emre Yiğit Taşdelen · MIT License</sub>
