"""Tests for the new-format (post-2025, div-based) Amazon invoice parser.

Test structure
==============

Each test case consists of a pair of files in the testdata directory::

    testdata/source/amazon/new_format/<order-id>.html   # sanitized HTML
    testdata/source/amazon/new_format/<order-id>.json   # golden JSON

The golden JSON is produced by running the parser against the sanitized HTML
and capturing the output of ``amazon_invoice.to_json()``.  To regenerate a
golden file after a deliberate parser change, run::

    python -m beancount_import.source.amazon_invoice_new_test --regen <order-id>

or simply delete the ``.json`` file and re-run the test suite; the missing
golden will be written automatically and the test will pass on the next run.

Adding new test cases
=====================

1.  Sanitize your raw invoice::

        python -m beancount_import.source.amazon_invoice_new_sanitize \\
            ~/Downloads/<order-id>.html \\
            testdata/source/amazon/new_format/

2.  Generate the golden JSON::

        python -m beancount_import.source.amazon_invoice_new_test --regen <order-id>

3.  Inspect both files, then add the order ID to the ``@pytest.mark.parametrize``
    list below under the appropriate comment.

Covered scenarios
=================

The parametrize list below documents which invoice variant each fixture
exercises.  When adding a new fixture, add a short comment describing what
makes it distinct (e.g. gift card, multi-shipment, posttax discount, etc.).
"""

import collections
import json
import os
import sys

import pytest

from . import amazon_invoice
from . import amazon_invoice_new

# Root of the new-format test fixtures, relative to this file's location.
# Layout: testdata/source/amazon/new_format/<order-id>.{html,json}
testdata_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..', '..', 'testdata', 'source', 'amazon', 'new_format'))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run_and_compare(name: str, locale=amazon_invoice.Locale_en_US) -> None:
    """Parse *name*.html, compare to *name*.json, regenerate if missing."""
    source_path = os.path.join(testdata_dir, name + '.html')
    json_path   = os.path.join(testdata_dir, name + '.json')

    invoice = amazon_invoice_new.parse_new_format_invoice(
        source_path, locale=locale)
    actual     = amazon_invoice.to_json(invoice)
    actual_str = json.dumps(actual, indent=4)

    if not os.path.exists(json_path):
        # Golden file is missing: write it so the developer can inspect it,
        # then fail with an instructive message.
        with open(json_path, 'w', encoding='utf-8') as f:
            f.write(actual_str + '\n')
        pytest.fail(
            'Golden file was missing and has been written to:\n  %s\n'
            'Review it, then re-run the tests.' % json_path)

    with open(json_path, 'r', encoding='utf-8') as f:
        expected = json.load(f, object_pairs_hook=collections.OrderedDict)
    expected_str = json.dumps(expected, indent=4)

    if expected_str != actual_str:
        # Print the actual output so pytest's captured output shows the diff.
        print(actual_str)
    assert expected_str == actual_str


# ---------------------------------------------------------------------------
# en_US new-format fixtures
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name', [
    # multi-shipment order, third-party sellers, shipping & handling $0,
    # single Visa payment, no posttax adjustments
    '658-7171324-9300534',
    '200-5551393-4974620',
    '358-5356143-4051025',

    # TODO: add more order IDs here as you sanitize additional invoices, e.g.:
    #   '<order-id>',   # single-item order
    #   '<order-id>',   # posttax gift-card discount
    #   '<order-id>',   # Subscribe & Save discount
])
def test_parsing_new_format_en_US(name: str) -> None:
    _run_and_compare(name, locale=amazon_invoice.Locale_en_US)


# ---------------------------------------------------------------------------
# Regeneration CLI
# ---------------------------------------------------------------------------

def _regen(names: list, locale=amazon_invoice.Locale_en_US) -> None:
    """Overwrite golden JSON files from current parser output."""
    os.makedirs(testdata_dir, exist_ok=True)
    for name in names:
        source_path = os.path.join(testdata_dir, name + '.html')
        json_path   = os.path.join(testdata_dir, name + '.json')
        invoice     = amazon_invoice_new.parse_new_format_invoice(
            source_path, locale=locale)
        output      = json.dumps(amazon_invoice.to_json(invoice), indent=4)
        with open(json_path, 'w', encoding='utf-8') as f:
            f.write(output + '\n')
        print('Wrote: %s' % json_path)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(
        description='Regenerate golden JSON files for new-format invoice tests.')
    ap.add_argument(
        '--regen', metavar='ORDER_ID', nargs='+',
        help='Order ID(s) whose golden JSON should be regenerated from the '
             'current parser output.')
    ap.add_argument(
        '--locale', default='en_US',
        help='Locale to use when regenerating (default: en_US).')
    args = ap.parse_args()
    if args.regen:
        locale_cls = amazon_invoice.LOCALES[args.locale]
        _regen(args.regen, locale=locale_cls)
    else:
        ap.print_help()
        sys.exit(1)
