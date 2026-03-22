"""Parses Amazon.com order invoices in the new (post-2025) div-based HTML format.

Amazon migrated away from a table-based invoice layout to a component-based div
layout that uses Amazon's internal UI framework (a-box, a-fixed-right-grid, and
data-component attributes).  The key structural differences from the old format
that this module handles are:

- The page <title> is simply "Order Details" rather than containing the order ID.
- There are no <table> elements.  All layout is done with nested <div>s.
- The order date and order ID live in a shared header block identified by
  the text node "Order placed".
- Each shipment is a ``data-component="purchasedItems"`` container; within it,
  each item has its own ``data-component`` divs for title, seller, price, etc.
- Summary totals (subtotal, shipping, tax, grand total) are ``od-line-item-row``
  divs, each with an ``od-line-item-row-label`` and ``od-line-item-row-content``
  child.
- The payment method is in a ``pmts-payments-instrument-detail-box-...`` span.
- Shipped dates are not reliably present in the static saved HTML (the shipment
  status section is populated by JavaScript at runtime).  ``shipped_date`` is
  therefore set to ``None`` for all shipments.

This module produces the same ``Order`` / ``Shipment`` / ``Item`` /
``CreditCardTransaction`` named-tuples as ``amazon_invoice``, so the rest of the
pipeline (``amazon.py``, ``amazon_invoice_test.py``, etc.) needs no changes.
"""

from typing import List, Optional, Dict
import re
import os
import datetime
import logging

import bs4
from bs4 import BeautifulSoup, Tag

import dateutil.parser

from beancount.core.amount import Amount
from beancount.core.number import D

from .amazon_invoice import (
    Locale_Data,
    Locale_en_US,
    LOCALES,
    Order,
    Shipment,
    Item,
    CreditCardTransaction,
    Adjustment,
    Errors,
    add_amount,
    reduce_amounts,
    reduce_amounts_may_return_none,
)

logger = logging.getLogger('amazon_invoice_new')


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def is_new_format(soup: BeautifulSoup) -> bool:
    """Return True when the soup looks like the new div-based invoice format.

    Two independent signals, either of which is sufficient:
    - The page title is exactly "Order Details" (old format embeds the order ID).
    - There are no <table> elements (the new layout is entirely div-based).
    """
    title_tag = soup.find('title')
    if title_tag and title_tag.text.strip() == 'Order Details':
        return True
    if len(soup.find_all('table')) == 0:
        return True
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_od_line_item(right_col: Tag, label_pattern: str) -> Optional[str]:
    """Return the value text of the first ``od-line-item-row`` whose label
    matches *label_pattern* (full-match, case-insensitive)."""
    for row in right_col.find_all('div', class_='od-line-item-row'):
        label_div = row.find('div', class_='od-line-item-row-label')
        value_div = row.find('div', class_='od-line-item-row-content')
        if label_div and value_div:
            if re.fullmatch(label_pattern, label_div.text.strip(), re.I):
                return value_div.text.strip()
    return None


def _get_all_od_line_items(
        right_col: Tag,
        label_pattern: str) -> List[tuple]:
    """Return ``(label, value)`` pairs for every ``od-line-item-row`` whose
    label matches *label_pattern* (full-match, case-insensitive)."""
    results = []
    for row in right_col.find_all('div', class_='od-line-item-row'):
        label_div = row.find('div', class_='od-line-item-row-label')
        value_div = row.find('div', class_='od-line-item-row-content')
        if label_div and value_div:
            label = label_div.text.strip().rstrip(':')
            if re.fullmatch(label_pattern, label_div.text.strip(), re.I):
                results.append((label, value_div.text.strip()))
    return results


# ---------------------------------------------------------------------------
# Order-level metadata
# ---------------------------------------------------------------------------

def _parse_order_metadata(soup: BeautifulSoup,
                           locale: Locale_Data
                           ) -> tuple:  # (order_id, order_date)
    """Extract order ID and order date from the header block.

    The new format renders a block whose text (after stripping whitespace)
    reads like::

        Order placed
        April 6, 2025
        Order #
        113-2087606-5082648

    We locate the "Order placed" text node and walk up to the shared container
    div that holds both the date and the order number.
    """
    op_node = soup.find(string='Order placed')
    if op_node is None:
        raise ValueError("Could not find 'Order placed' text node in new-format invoice")

    # The node lives at: "Order placed" -> <span> -> <div> -> <div (container)>
    container = op_node.parent.parent.parent
    lines = [l.strip() for l in container.get_text('\n').splitlines() if l.strip()]
    # Expected lines: ['Order placed', '<date>', 'Order #', '<order-id>']
    # Be resilient to minor variations in ordering.
    order_date = None
    order_id = None
    for i, line in enumerate(lines):
        if re.match(r'\d{3}-\d{7}-\d{7}', line):
            order_id = line
        elif line == 'Order placed' and i + 1 < len(lines):
            try:
                order_date = locale.parse_date(lines[i + 1])
            except Exception:
                pass

    if order_id is None:
        # Fallback: derive from the filename via the soup's source path (not
        # available here), or raise a clear error.
        raise ValueError(
            "Could not extract order ID from new-format invoice header. "
            "Lines found: %r" % lines)
    if order_date is None:
        raise ValueError(
            "Could not extract order date from new-format invoice header. "
            "Lines found: %r" % lines)

    return order_id, order_date


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def _parse_items(soup: BeautifulSoup,
                 locale: Locale_Data,
                 errors: Errors) -> List[List[Item]]:
    """Return a list-of-lists: one inner list of ``Item`` objects per shipment
    container (``data-component="purchasedItems"``).

    In the new format each shipment box is a separate ``a-box`` div that
    contains a ``data-component="purchasedItems"`` section.  Within that
    section every individual item is an ``a-fixed-left-grid`` div with
    dedicated ``data-component`` children for title, seller, price, etc.

    Notes on missing fields:
    - **quantity**: The ``data-component="quantity"`` div is present but
      always empty in saved HTML (populated by JS).  We default to 1.
    - **condition**: Similarly always empty; set to ``None``.
    - **price**: We prefer the ``a-offscreen`` span (screen-reader text,
      no duplicate rendering artefact) inside the ``unitPrice`` component.
    """
    all_shipment_items = []  # type: List[List[Item]]

    for shipment_div in soup.find_all(attrs={'data-component': 'purchasedItems'}):
        shipment_items = []  # type: List[Item]

        for item_grid in shipment_div.find_all('div', class_='a-fixed-left-grid'):
            title_div  = item_grid.find(attrs={'data-component': 'itemTitle'})
            seller_div = item_grid.find(attrs={'data-component': 'orderedMerchant'})
            price_div  = item_grid.find(attrs={'data-component': 'unitPrice'})

            if title_div is None or price_div is None:
                errors.append(
                    "Skipping item grid: missing title or price component. "
                    "Snippet: %r" % item_grid.text.strip()[:80])
                continue

            # --- title / URL ---
            a_tag = title_div.find('a')
            description = a_tag.text.strip() if a_tag else title_div.text.strip()
            description = re.sub(r'\s+', ' ', description)

            # --- seller ---
            sold_by = None  # type: Optional[str]
            if seller_div:
                # Prefer the anchor text (third-party seller links); fall back
                # to stripping the "Sold by: " prefix from the raw text.
                a_seller = seller_div.find('a')
                if a_seller:
                    sold_by = a_seller.text.strip()
                else:
                    raw = seller_div.text.strip()
                    sold_by = re.sub(r'^Sold by:\s*', '', raw, flags=re.I).strip() or None

            # --- price ---
            # The unitPrice span renders the dollar amount twice: once in an
            # ``a-offscreen`` span (clean) and once visible (may be duplicated
            # in the text).  Prefer the offscreen version.
            offscreen = price_div.find('span', class_='a-offscreen')
            price_text = offscreen.text.strip() if offscreen else price_div.text.strip()
            # If still duplicated (e.g. "$32.31$32.31"), take the first match.
            price_match = re.search(r'\$[\d,.]+', price_text)
            if price_match:
                price_text = price_match.group(0)
            try:
                price = locale.parse_amount(price_text)
            except Exception as exc:
                errors.append(
                    "Could not parse item price %r for %r: %s" %
                    (price_text, description[:60], exc))
                continue

            shipment_items.append(Item(
                quantity=D('1'),
                description=description,
                sold_by=sold_by,
                condition=None,  # not available in static HTML
                price=price,
            ))

        if shipment_items:
            all_shipment_items.append(shipment_items)

    return all_shipment_items


# ---------------------------------------------------------------------------
# Summary totals
# ---------------------------------------------------------------------------

def _parse_summary(soup: BeautifulSoup,
                   locale: Locale_Data,
                   errors: Errors,
                   locale_cls: Locale_Data) -> dict:
    """Parse the order-level summary block (right column of the outermost
    ``a-fixed-right-grid``).

    Returns a dict with keys:
        items_subtotal, pretax_adjustments, total_before_tax,
        tax, grand_total, posttax_adjustments
    """
    outer_grid = soup.find('div', class_='a-fixed-right-grid')
    if outer_grid is None:
        raise ValueError("Could not find outer a-fixed-right-grid in new-format invoice")

    right_col = outer_grid.find('div', class_=re.compile(r'\ba-col-right\b'))
    if right_col is None:
        raise ValueError("Could not find right column of outer grid in new-format invoice")

    def get_amount(label_pattern: str) -> Optional[Amount]:
        raw = _get_od_line_item(right_col, label_pattern)
        if raw is None:
            return None
        try:
            return locale.parse_amount(raw)
        except Exception as exc:
            errors.append("Could not parse amount for %r (%r): %s" %
                          (label_pattern, raw, exc))
            return None

    # Fixed known labels
    items_subtotal   = get_amount(locale_cls.items_subtotal)
    total_before_tax = get_amount(locale_cls.total_before_tax)
    grand_total      = get_amount(locale_cls.regular_total_order)

    # Tax: Estimated tax to be collected (en_US) or equivalent
    tax_raw = _get_od_line_item(right_col, locale_cls.regular_estimated_tax)
    tax_amount = None  # type: Optional[Amount]
    if tax_raw:
        try:
            tax_amount = locale.parse_amount(tax_raw)
        except Exception as exc:
            errors.append("Could not parse tax %r: %s" % (tax_raw, exc))

    # Pre-tax adjustments (shipping & handling, discounts, etc.)
    pretax_adjustments = []  # type: List[Adjustment]
    for label, value in _get_all_od_line_items(
            right_col, locale_cls.pretax_adjustment_fields_pattern):
        try:
            pretax_adjustments.append(
                Adjustment(description=label, amount=locale.parse_amount(value)))
        except Exception as exc:
            errors.append("Could not parse pretax adjustment %r=%r: %s" %
                          (label, value, exc))

    # Post-tax adjustments (gift cards, reward points, etc.)
    posttax_adjustments = []  # type: List[Adjustment]
    for label, value in _get_all_od_line_items(
            right_col, locale_cls.posttax_adjustment_fields_pattern):
        try:
            posttax_adjustments.append(
                Adjustment(description=label, amount=locale.parse_amount(value)))
        except Exception as exc:
            errors.append("Could not parse posttax adjustment %r=%r: %s" %
                          (label, value, exc))

    return dict(
        items_subtotal=items_subtotal,
        pretax_adjustments=pretax_adjustments,
        total_before_tax=total_before_tax,
        tax_amount=tax_amount,
        grand_total=grand_total,
        posttax_adjustments=posttax_adjustments,
    )


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

def _parse_payment(soup: BeautifulSoup,
                   order_date: datetime.date,
                   grand_total: Optional[Amount],
                   errors: Errors) -> List[CreditCardTransaction]:
    """Extract a payment transaction from the ``pmts-*`` span.

    The new format shows only the card type and last-four digits with no
    per-transaction breakdown, so we synthesise a single transaction for the
    grand total (mirroring the fallback behaviour of the old parser).

    Handles two text patterns:
    - "Visa ending in 1378"                  (new format)
    - "Amazon Visa | Last 4 digits: 1234"    (possible variant)
    """
    if grand_total is None:
        errors.append("Cannot create payment transaction: grand total is unknown")
        return []

    pay_span = soup.find(
        True,
        class_=re.compile(r'pmts-payments-instrument-detail-box'))
    if pay_span is None:
        errors.append("Could not find payment method span in new-format invoice")
        return []

    pay_text = pay_span.text.strip()

    # Pattern 1: "Visa ending in 1378"
    m = re.match(r'^(.*?)\s+ending in\s+(\d{4})$', pay_text, re.I)
    if m:
        return [CreditCardTransaction(
            date=order_date,
            card_description=m.group(1).strip(),
            card_ending_in=m.group(2),
            amount=grand_total,
        )]

    # Pattern 2: "Amazon Visa | Last 4 digits: 1234"
    m = re.search(r'([^|]+?)\s*\|\s*Last\s+(?:4\s+)?digits:\s*(\d{4})', pay_text, re.I)
    if m:
        return [CreditCardTransaction(
            date=order_date,
            card_description=m.group(1).strip(),
            card_ending_in=m.group(2),
            amount=grand_total,
        )]

    errors.append(
        "Could not parse payment method from %r; "
        "no credit card transaction will be recorded" % pay_text)
    return []


# ---------------------------------------------------------------------------
# Consistency checks
# ---------------------------------------------------------------------------

def _check_totals(summary: dict,
                  all_items: List[List[Item]],
                  locale: Locale_Data,
                  errors: Errors) -> None:
    """Perform the same arithmetic cross-checks the old parser does."""
    items_subtotal   = summary['items_subtotal']
    pretax_adjs      = summary['pretax_adjustments']
    total_before_tax = summary['total_before_tax']
    tax_amount       = summary['tax_amount']
    posttax_adjs     = summary['posttax_adjustments']
    grand_total      = summary['grand_total']

    # Sum of all item prices across shipments
    flat_items = [item for shipment in all_items for item in shipment]
    expected_subtotal = reduce_amounts_may_return_none(
        item.price for item in flat_items)

    if (items_subtotal is not None and
            expected_subtotal is not None and
            expected_subtotal != items_subtotal):
        errors.append(
            'expected items subtotal %r but invoice shows %r' %
            (expected_subtotal, items_subtotal))

    # items_subtotal + pretax adjustments = total_before_tax
    effective_subtotal = items_subtotal or expected_subtotal
    if effective_subtotal is not None:
        pretax_parts = [effective_subtotal] + [a.amount for a in pretax_adjs]
        expected_tbt = reduce_amounts(pretax_parts)
        if total_before_tax is not None and expected_tbt != total_before_tax:
            errors.append(
                'expected total before tax %r but invoice shows %r' %
                (expected_tbt, total_before_tax))

    # total_before_tax + tax + posttax adjustments = grand_total
    if total_before_tax is not None and grand_total is not None:
        posttax_parts = [total_before_tax]
        if tax_amount is not None:
            posttax_parts.append(tax_amount)
        posttax_parts += [a.amount for a in posttax_adjs]
        expected_grand = reduce_amounts(posttax_parts)
        if expected_grand != grand_total:
            errors.append(
                'expected grand total %r but invoice shows %r' %
                (expected_grand, grand_total))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_new_format_invoice(path: str,
                              locale: Locale_Data = Locale_en_US) -> Order:
    """Parse a new-format (div-based) Amazon order invoice and return an
    ``Order`` named-tuple compatible with the rest of the beancount-import
    Amazon pipeline.

    Raises ``ValueError`` if essential structural elements cannot be found.
    """
    errors = []  # type: Errors

    with open(path, 'rb') as f:
        soup = BeautifulSoup(f.read(), 'lxml')

    logger.debug('Parsing new-format invoice: %s', path)

    # 1. Order metadata
    order_id, order_date = _parse_order_metadata(soup, locale)
    logger.debug('order_id=%s  order_date=%s', order_id, order_date)

    # 2. Items (grouped by shipment box)
    all_shipment_items = _parse_items(soup, locale, errors)
    logger.debug('%d shipment box(es), %d total item(s)',
                 len(all_shipment_items),
                 sum(len(s) for s in all_shipment_items))

    # 3. Summary totals (order level — not per-shipment in the new format)
    summary = _parse_summary(soup, locale, errors, locale)
    grand_total      = summary['grand_total']
    items_subtotal   = summary['items_subtotal']
    total_before_tax = summary['total_before_tax']
    tax_amount       = summary['tax_amount']
    pretax_adjs      = summary['pretax_adjustments']
    posttax_adjs     = summary['posttax_adjustments']

    # 4. Consistency checks
    _check_totals(summary, all_shipment_items, locale, errors)

    # 5. Build Shipment objects.
    #
    # The new format provides NO per-shipment financial breakdown — all totals
    # live at order level.  We therefore set items_subtotal=None on each
    # Shipment so that amazon.py's existing logic (which checks
    # ``all([s.items_subtotal is None for s in invoice.shipments])``) correctly
    # identifies this as a new-format invoice and applies tax / pretax
    # adjustments at the order level rather than the shipment level.
    shipments = []  # type: List[Shipment]
    for items in all_shipment_items:
        shipments.append(Shipment(
            shipped_date=None,      # not available in static HTML
            items=items,
            items_subtotal=None,    # signal for amazon.py order-level handling
            pretax_adjustments=[],
            total_before_tax=None,
            posttax_adjustments=[],
            tax=[],
            total=None,
            errors=[],
        ))

    if not shipments:
        msg = ('New-format invoice contained no parseable shipments. '
               'This may indicate a new page variant. '
               'Consider opening an issue at jbms/beancount-import on github.')
        logger.warning(msg)
        errors.append(msg)

    # 6. Payment
    credit_card_transactions = _parse_payment(
        soup, order_date, grand_total, errors)

    # 7. Tax: emit as an order-level amount (not per-shipment) so that
    #    amazon.py adds a "Sales Tax" posting when tax > 0.
    tax_for_order = tax_amount  # may be None if not found or zero

    logger.debug('Finished parsing new-format invoice %s', path)

    return Order(
        order_id=order_id,
        order_date=order_date,
        shipments=shipments,
        credit_card_transactions=credit_card_transactions,
        pretax_adjustments=pretax_adjs,
        tax=tax_for_order,
        posttax_adjustments=posttax_adjs,
        errors=errors,
    )
