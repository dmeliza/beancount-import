# Adding New-Format Amazon Invoice Test Fixtures

This document explains how to place the new sanitizer, test module, and golden
files into the beancount-import source tree, and how to add additional invoice
fixtures in the future.

---

## 1. Place the new source files

Copy the four deliverables from this changeset into the package alongside the
existing Amazon modules:

```
beancount_import/source/
    amazon.py                        ← replace with updated version
    amazon_invoice.py                ← unchanged
    amazon_invoice_new.py            ← new parser (add this)
    amazon_invoice_new_sanitize.py   ← new sanitizer (add this)
    amazon_invoice_new_test.py       ← new test module (add this)
    amazon_invoice_sanitize.py       ← unchanged
    amazon_invoice_test.py           ← unchanged
```

The new sanitizer imports two helpers from the old one
(`sanitize_order_ids`, `sanitize_credit_card`, `make_random_number_replacement`),
so both files must coexist in the same package.

---

## 2. Create the testdata directory

New-format fixtures live in their own subdirectory so they do not interfere
with the old-format fixtures already checked in:

```
testdata/source/amazon/
    new_format/           ← create this directory
        .gitkeep          ← optional, to keep the empty dir in git
```

```bash
mkdir -p testdata/source/amazon/new_format
```

---

## 3. Install the first fixture

The sanitized HTML and golden JSON for order `113-2087606-5082648` (a
multi-shipment en_US order with two shipment boxes and three items across them)
are included in this changeset.

```
testdata/source/amazon/new_format/
    113-2087606-5082648.html    ← sanitized HTML (produce per §4 below)
    113-2087606-5082648.json    ← golden JSON    (provided in changeset)
```

**Producing the sanitized HTML from your raw download:**

```bash
python -m beancount_import.source.amazon_invoice_new_sanitize \
    ~/Downloads/113-2087606-5082648.html \
    testdata/source/amazon/new_format/
```

This writes `testdata/source/amazon/new_format/113-2087606-5082648.html`
with all PII removed.

> **Important:** the sanitizer randomises the order-ID digits, so the output
> filename will contain *different* digits than the input. The golden JSON was
> generated against the *pre-sanitized* file (real digits) and then the CC
> digits were manually updated to `1234` to match what the sanitizer produces.
> If you re-sanitize, regenerate the golden JSON as described in §5.

**Verifying the first fixture runs correctly:**

```bash
pytest beancount_import/source/amazon_invoice_new_test.py -v
```

Expected output:

```
PASSED beancount_import/source/amazon_invoice_new_test.py::test_parsing_new_format_en_US[113-2087606-5082648]
```

---

## 4. Sanitizing additional raw invoices

For each new invoice you want to add as a test fixture:

### 4a. Download the raw invoice

In your browser, go to **Your Orders → Invoice** (not "Order Details") for the
order, let the page fully load, then **File → Save Page As → Webpage, Complete**
(or your browser's equivalent). Rename the saved file to `<order-id>.html`
where `<order-id>` is the exact order number (e.g. `123-4567890-1234567.html`).

### 4b. Run the new-format sanitizer

```bash
python -m beancount_import.source.amazon_invoice_new_sanitize \
    ~/Downloads/<order-id>.html \
    testdata/source/amazon/new_format/ \
    --credit-card-digits 1234
```

The sanitizer will:

- Strip all `<script>`, `<style>`, `<link>`, `<noscript>`, `<img>`, and
  `<input>` tags and all HTML comments.
- Clear the shipping address block (`data-component="shippingAddress"`),
  removing the recipient's name, street address, city, state, and ZIP.
- Strip `seller=<ID>` query parameters from third-party seller profile links
  (seller display names in the anchor text are preserved).
- Normalise randomised `pmts-portal-root-*` and
  `pmts-portal-components-pp-*` class names to stable placeholders.
- Strip all hrefs except `/dp/<ASIN>` product detail links.
- Randomise all order-ID digit sequences consistently across the document
  (so the same ID appears as the same fake ID everywhere).
- Replace all credit card last-four-digit sequences with `1234`.

### 4c. Manually inspect the output

```bash
# Quick sanity check — none of these should print anything
grep -i 'your name\|your street\|your city\|your zip' \
    testdata/source/amazon/new_format/<sanitized-order-id>.html

# Check for any remaining real credit card digits
grep -oE 'ending in [0-9]{4}' \
    testdata/source/amazon/new_format/<sanitized-order-id>.html
# Should print only: ending in 1234

# Check for seller ID leakage
grep -oE 'seller=[A-Z0-9]+' \
    testdata/source/amazon/new_format/<sanitized-order-id>.html
# Should print nothing
```

Always open the file in a text editor and skim it before committing.

### 4d. Generate the golden JSON

```bash
python -m beancount_import.source.amazon_invoice_new_test \
    --regen <sanitized-order-id>
```

This runs the parser against the sanitized HTML and writes
`testdata/source/amazon/new_format/<sanitized-order-id>.json`.
Inspect the JSON to confirm the parser extracted everything correctly:

```bash
cat testdata/source/amazon/new_format/<sanitized-order-id>.json
```

Fields to check:

| Field | What to verify |
|---|---|
| `order_id` | Matches the sanitized (randomised) digits in the filename |
| `order_date` | Correct date |
| `shipments[*].items` | All items present, descriptions match, prices correct |
| `shipments[*].items_subtotal` | Must be `null` (new format; order-level accounting) |
| `credit_card_transactions[0].card_ending_in` | `"1234"` (sanitized) |
| `credit_card_transactions[0].amount` | Matches the Grand Total on the invoice |
| `pretax_adjustments` | Shipping & Handling line present (even if $0.00) |
| `tax` | Correct estimated tax amount |
| `errors` | Should be `[]` for a clean invoice |

### 4e. Register the fixture in the test parametrize list

Open `amazon_invoice_new_test.py` and add the sanitized order ID to the
`@pytest.mark.parametrize` list in `test_parsing_new_format_en_US`, with a
brief comment explaining what makes this fixture distinct:

```python
@pytest.mark.parametrize('name', [
    # multi-shipment, third-party sellers, Visa, no posttax adjustments
    '113-2087606-5082648',

    # single-item, Subscribe & Save discount
    '<your-new-sanitized-order-id>',
])
def test_parsing_new_format_en_US(name: str) -> None:
    _run_and_compare(name, locale=amazon_invoice.Locale_en_US)
```

### 4f. Run the full test suite

```bash
pytest beancount_import/source/amazon_invoice_new_test.py \
       beancount_import/source/amazon_invoice_test.py \
       -v
```

All old-format tests must continue to pass unchanged.

---

## 5. Regenerating a golden file after a deliberate parser change

If you intentionally change `amazon_invoice_new.py` in a way that alters
parser output, regenerate the affected golden files and review the diff:

```bash
# Regenerate one fixture
python -m beancount_import.source.amazon_invoice_new_test \
    --regen <order-id>

# Regenerate all fixtures at once
python -m beancount_import.source.amazon_invoice_new_test \
    --regen 113-2087606-5082648 <other-id> <other-id> ...

# Review what changed
git diff testdata/source/amazon/new_format/
```

Commit the updated golden files together with the parser change so the test
history stays coherent.

---

## 6. Priority invoice variants to add

The table below lists scenarios that are well-covered by the old-format test
suite but not yet covered for the new format.  Add fixtures for these as you
encounter orders of each type in your own history.

| Scenario | What to look for |
|---|---|
| Single-item order | Simplest case; good baseline |
| Posttax gift-card discount | `posttax_adjustments` non-empty |
| Rewards Points applied | `posttax_adjustments` with "Rewards Points" |
| Subscribe & Save discount | Pretax adjustment with discount line |
| Third-party seller only | No Amazon.com-fulfilled items |
| Free-shipping coupon | Pretax adjustment cancels S&H charge |
| Multiple CC transactions | `credit_card_transactions` length > 1 (if/when the new format adds this) |
| Digital order in new format | If Amazon ever applies the new layout to digital orders |

---

## 7. File locations summary

```
beancount_import/
  source/
    amazon.py                          updated (format detection + routing)
    amazon_invoice_new.py              new parser
    amazon_invoice_new_sanitize.py     new sanitizer
    amazon_invoice_new_test.py         new test module

testdata/
  source/
    amazon/
      new_format/
        113-2087606-5082648.html       first fixture (sanitize from raw download)
        113-2087606-5082648.json       golden JSON (provided)
```
