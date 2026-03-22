"""Strips identifying information from a new-format Amazon.com order HTML file.

Amazon's post-2025 invoice format uses a component-based div layout
(``data-component`` attributes, ``a-box`` / ``a-fixed-right-grid`` containers)
rather than the table-based layout handled by ``amazon_invoice_sanitize.py``.
This module deals with the PII that is unique to the new structure while
reusing the format-agnostic helpers from the original sanitizer.

Specifically, beyond what the original sanitizer already handles, this module:

* Clears the ``data-component="shippingAddress"`` subtree, which contains the
  recipient's full name, street address, city, state, and ZIP code as plain
  text nodes (not encoded in a URL or attribute, so regex address stripping
  does not catch it).

* Strips ``seller=<ID>`` query parameters from ``/gp/aag/`` seller-profile
  hrefs, which expose Amazon seller IDs.  The seller's display name (anchor
  text) is preserved so the invoice remains human-readable.

* Replaces the randomised session-scoped class names that appear on
  ``pmts-portal-root-*`` and ``pmts-portal-components-pp-*`` elements.  These
  tokens are not personal data, but they are unique per page load and would
  make diff-based test comparisons noisy.

* Keeps ``/dp/<ASIN>`` product hrefs intact (they are product identifiers, not
  personal data), while removing all other hrefs as the original sanitizer does.

WARNING: This may not strip all identifying information.  Always manually
inspect the sanitized output before committing it to a public repository.
"""

from typing import Optional, Dict, Tuple
import os
import random
import re

import bs4

# ---------------------------------------------------------------------------
# Reuse the format-agnostic helpers from the original sanitizer.
# ---------------------------------------------------------------------------
from .amazon_invoice_sanitize import (
    make_random_number_replacement,
    sanitize_order_ids,
    sanitize_credit_card,
)


# ---------------------------------------------------------------------------
# New-format-specific helpers
# ---------------------------------------------------------------------------

def _remove_tag(soup: bs4.BeautifulSoup, tag: str) -> None:
    for x in soup.find_all(tag):
        x.extract()


def sanitize_shipping_address(soup: bs4.BeautifulSoup) -> None:
    """Replace the content of the shippingAddress component with a placeholder.

    The new format renders the recipient's name and address as plain text nodes
    inside a ``data-component="shippingAddress"`` div::

        <div data-component="shippingAddress">
          <h5>Ship to</h5>
          <ul>
            <li><span class="a-list-item">Full Name</span></li>
            <li><span class="a-list-item">123 Street Rd<br/>City, ST 00000</span></li>
            <li><span class="a-list-item">United States</span></li>
          </ul>
        </div>

    We blank all ``<li>`` text content inside this component while keeping the
    structural tags, so the invoice layout remains parseable.
    """
    addr_div = soup.find(attrs={'data-component': 'shippingAddress'})
    if addr_div is None:
        return
    for li in addr_div.find_all('li'):
        li.clear()


def sanitize_seller_hrefs(soup: bs4.BeautifulSoup) -> None:
    """Remove ``seller=<ID>`` from third-party seller profile hrefs.

    Seller profile links have the form ``/gp/aag/main?ie=UTF8&seller=A1B2C3``.
    The seller ID is not personal data but may be considered quasi-identifying.
    We keep the anchor text (the seller's display name) and the ``/gp/aag/``
    path but strip the ``seller`` query parameter.
    """
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/gp/aag/' in href:
            # Remove seller= parameter; drop the whole href if nothing useful remains
            cleaned = re.sub(r'[?&]seller=[^&]*', '', href)
            # Strip trailing ? or & left behind
            cleaned = re.sub(r'[?&]$', '', cleaned)
            if cleaned and cleaned != href:
                a['href'] = cleaned
            else:
                del a['href']


def sanitize_pmts_session_classes(soup: bs4.BeautifulSoup) -> None:
    """Replace random session-scoped class names on pmts-portal-* elements.

    Amazon injects class names of the form ``pmts-portal-root-<token>`` and
    ``pmts-portal-components-pp-<token>-<N>`` that are unique to each page
    load.  These make byte-for-byte test comparisons unreliable.  We replace
    each distinct token with a deterministic placeholder so sanitized files
    are stable across re-downloads.
    """
    token_map = {}  # type: Dict[str, str]
    counter = [0]

    def stable_token(original: str) -> str:
        if original not in token_map:
            counter[0] += 1
            token_map[original] = 'SANITIZED%d' % counter[0]
        return token_map[original]

    root_pattern = re.compile(r'^(pmts-portal-root-)(\w+)$')
    comp_pattern = re.compile(r'^(pmts-portal-components-pp-)(\w+)(-\d+)$')

    for tag in soup.find_all(True):
        classes = tag.get('class')
        if not classes:
            continue
        new_classes = []
        for cls in classes:
            m = root_pattern.match(cls)
            if m:
                new_classes.append(m.group(1) + stable_token(m.group(2)))
                continue
            m = comp_pattern.match(cls)
            if m:
                new_classes.append(
                    m.group(1) + stable_token(m.group(2)) + m.group(3))
                continue
            new_classes.append(cls)
        tag['class'] = new_classes


def sanitize_non_product_hrefs(soup: bs4.BeautifulSoup) -> None:
    """Remove all hrefs except ``/dp/<ASIN>`` product detail links.

    Mirrors the behaviour of the original sanitizer: product links are kept
    because they are publicly reachable ASIN URLs (not personal data), while
    all other hrefs (order history, seller profiles, account pages, etc.) are
    stripped.  ``sanitize_seller_hrefs`` should be called first so that the
    seller-profile hrefs are already cleaned before this pass removes them.
    """
    for a in soup.find_all('a', href=True):
        if '/dp/' not in a['href']:
            del a['href']


# ---------------------------------------------------------------------------
# Main sanitize entry point
# ---------------------------------------------------------------------------

def sanitize_new_invoice(input_path: str,
                          output_path: str,
                          credit_card_digits: str = '1234') -> None:
    """Sanitize a new-format Amazon invoice HTML file.

    Reads *input_path*, applies all sanitization passes, and writes the result
    to *output_path*.  If *output_path* is a directory the sanitized file is
    written there using the (also sanitized) basename of *input_path*.

    Steps applied, in order:

    1. Parse with BeautifulSoup / lxml.
    2. Strip all ``<script>``, ``<style>``, ``<link>``, ``<noscript>``,
       ``<img>``, and ``<input>`` tags and all HTML comments.
    3. Clear the shipping address component (recipient name + postal address).
    4. Strip seller IDs from ``/gp/aag/`` hrefs.
    5. Normalise session-scoped ``pmts-portal-*`` class names.
    6. Strip all non-product hrefs (``/dp/`` links are kept).
    7. Randomise order ID digits consistently across the whole document
       (using the same routine as the original sanitizer so order IDs in
       filenames and in the HTML body are replaced with the same fake ID).
    8. Replace credit card last-four digits with *credit_card_digits*.
    """
    with open(input_path, 'rb') as fb:
        soup = bs4.BeautifulSoup(fb.read(), 'lxml')

    # --- Step 2: strip noisy / executable tags and comments ---
    for tag_name in ('script', 'style', 'link', 'noscript', 'img', 'input'):
        _remove_tag(soup, tag_name)
    for comment in soup.find_all(
            text=lambda text: isinstance(text, bs4.Comment)):
        comment.extract()

    # --- Step 3: shipping address ---
    sanitize_shipping_address(soup)

    # --- Step 4: seller profile hrefs ---
    sanitize_seller_hrefs(soup)

    # --- Step 5: pmts session class names ---
    sanitize_pmts_session_classes(soup)

    # --- Step 6: non-product hrefs ---
    sanitize_non_product_hrefs(soup)

    # --- Steps 7-8: text-level substitutions (order IDs and CC digits) ---
    new_output, order_id_replacements = sanitize_order_ids(str(soup))
    new_output = sanitize_credit_card(new_output, credit_card_digits)

    # --- Determine output path ---
    if os.path.isdir(output_path):
        output_name, _ = sanitize_order_ids(
            os.path.basename(input_path), order_id_replacements)
        output_path = os.path.join(output_path, output_name)

    with open(output_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(new_output)

    return output_path  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description='Sanitize a new-format Amazon order HTML file for use as '
                    'beancount-import test data.')
    ap.add_argument('invoice', help='Path to the raw invoice HTML file.')
    ap.add_argument(
        'output',
        help='Destination path (file) or directory.  '
             'If a directory, the sanitized file is written there with the '
             'same (but order-ID-randomised) filename.')
    ap.add_argument(
        '--credit-card-digits', default='1234',
        help='Replacement last-four digits for all credit card numbers '
             '(default: 1234).')
    args = ap.parse_args()
    sanitize_new_invoice(
        args.invoice, args.output,
        credit_card_digits=args.credit_card_digits)


if __name__ == '__main__':
    main()
