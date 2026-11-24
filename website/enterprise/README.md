# GemstoneAI Enterprise website

Static marketing pages for the enterprise suite. No build step or framework: plain HTML, one
stylesheet (`assets/styles.css`) and one small script (`assets/site.js`). Fonts load from Google
Fonts.

| Page | File |
| --- | --- |
| Suite overview | `index.html` |
| Comps | `comps.html` |
| Intake | `intake.html` |
| Estimate | `estimate.html` |
| Verify | `verify.html` |
| Index (GPI) | `price-index.html` |
| Developers / API overview | `developers.html` |
| Request a pilot | `pilot.html` |
| OpenAPI spec (copy of `docs/enterprise/openapi.json`) | `openapi.json` |

Preview locally: `python -m http.server -d website/enterprise 8080`, then open
http://localhost:8080.

The spec recommends hosting this separately from the consumer app, at
`enterprise.gemstoneaiapp.com` (or `data.gemstoneaiapp.com`), with no links into the consumer app.
These pages follow that: they link only to each other.

## Before publishing

- [ ] **Pilot form destination.** `pilot.html` opens the visitor's email app addressed to
      `enterprise@gemstoneaiapp.com`, a placeholder set in the form's `data-contact` attribute.
      Replace it with a real inbox, or point the form at your CRM or form endpoint.
- [ ] **Product status labels.** Comps says "Pilot available"; Intake, Estimate, Verify and Index say
      "Early access". Change them only when you can deliver what they promise.
- [ ] **API host.** The developer page and code samples use
      `https://api.gemstoneaiapp.com/enterprise/v1`, which is not deployed yet.
- [ ] **Brand.** The spec leaves open whether to use "GemstoneAI Enterprise" or a separate data
      brand. The pages use "GemstoneAI Enterprise".
- [ ] **Claims.** The pages deliberately contain no customer names or logos, accuracy figures,
      certifications (such as SOC 2) or prices. Add them only once confirmed: for example, Jewelers
      Mutual as a named reference, or a measured accuracy number for Comps.

The sample valuation card on the overview and Comps pages uses real output from the API's sandbox
(synthetic stones). It is labeled as such; keep that label.

`openapi.json` must match `docs/enterprise/openapi.json`; a test fails if they drift. After API
changes, run `python -m enterprise.cli export-openapi` and copy the file here.
