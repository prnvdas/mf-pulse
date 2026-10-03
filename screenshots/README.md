# Portfolio screenshots

Drop a screenshot of your Groww or IndMoney **mutual funds holdings page** here
(GitHub: *Add file → Upload files*, commit to `main`). The `sync-screenshot`
workflow reads it, updates your invested amount and units, and deletes the image.

- The page must show each fund's **Invested**, **Current Value** and **Gain/Loss**
  (IndMoney "My Funds" does; for Groww use the page that lists all three).
- Capture it at normal size, not zoomed out; small or heavily compressed images
  may not be readable and will be rejected rather than guessed.
- Crop out your name, folio and account details.
- If anything doesn't add up (value ≠ invested + gain, or a figure is far from what
  the dashboard currently has) the run fails and **changes nothing**; the Actions
  log says why.
- The image is removed after processing but remains in git history, so keep this
  repository private.
