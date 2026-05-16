# Security Policy

## Supported Versions

The following versions of Pulsar are currently being supported with security updates:

| Version      | Supported          |
| ------------ | ------------------ |
| v2026.05.*   | :white_check_mark: |
| < v2026.05.0 | :x:                |

## Reporting a Vulnerability

We take the security of Pulsar seriously. If you believe you have found a security vulnerability, please do **not** report it via a public issue or pull request. Instead, please follow the process below.

### Where to Report

Please report security vulnerabilities by sending an email to **security@planetic.ai** (or the email of the current maintainer).

Your report should include:

- A description of the vulnerability.
- The potential impact of the vulnerability.
- Step-by-step instructions to reproduce the issue.
- Any potential fix or mitigation (if known).

### Our Process

Once a report is received:

1.  **Acknowledgment**: We will acknowledge receipt of your report within 48 hours.
2.  **Investigation**: We will investigate the issue and determine its severity.
3.  **Communication**: We will keep you informed of our progress throughout the process.
4.  **Resolution**: If the vulnerability is confirmed, we will work on a fix and release a security update.
5.  **Disclosure**: We will coordinate with you to determine an appropriate disclosure timeline. We generally aim for a public advisory alongside the release of the fix.

## Security Best Practices for Users

- **Protect your `APP_ACCESS_TOKEN`**: This token provides full access to the application. Ensure it is stored securely and never shared.
- **Secure your Service Account Key**: The Google Drive service account JSON file should be treated as highly sensitive.
- **Run in a Secure Environment**: Ensure your Docker host is properly secured and that network access to the application is restricted (e.g., via a VPN or reverse proxy with authentication).
- **Keep Pulsar Updated**: Regularly pull the latest images or code to benefit from security improvements and bug fixes.

Thank you for helping keep Pulsar secure!
