# Self-hosted frontend fonts

These Latin-subset WOFF2 files replace the previous `next/font/google`
imports so production builds do not need network access to Google Fonts.

The files were sourced from the Fontsource 5.3.0 packages, which mirror the
corresponding Google Fonts releases:

- Fraunces v38 (`@fontsource-variable/fraunces`)
- Public Sans v21 (`@fontsource-variable/public-sans`)
- IBM Plex Mono v20 (`@fontsource/ibm-plex-mono`)
- Source Serif 4 v14 (`@fontsource-variable/source-serif-4`)
- Yellowtail v25 (`@fontsource/yellowtail`)

Each family directory contains the license distributed with its source
package.
