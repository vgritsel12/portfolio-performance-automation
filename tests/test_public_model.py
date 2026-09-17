# Existing self-contained tests extracted from the original suite.
from __future__ import annotations
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from renderer.portfolio_xml_model import AmbiguousReferenceError, CyclicReferenceError, DisplayCategory, MONEY_DIVIDER, PortfolioModel, PortfolioModelError, PricePoint, QUOTE_DIVIDER, ReferenceModeError, ReferenceTypeError, Security, SHARE_DIVIDER, SourceLocation, TransferStatus, UnresolvedReferenceError, UnsupportedVersionError, WEIGHT_DIVIDER, XmlSecurityError, money_value, normalize_transactions, quote_value, rate_value, share_value, weight_value, _ReferenceResolver
from renderer.xstream_reference_adapter import xpath_compatible_xml
from renderer.portfolio_valuation import PriceStatus, ReconciliationStatus, build_valuation, classify_technical_account, select_price

def minimal_xml(body: str='') -> str:
    return f'<client><version>70</version><baseCurrency>USD</baseCurrency>{body}<securities/><accounts/><portfolios/><taxonomies/><dashboards/></client>'

class TemporaryXmlMixin:

    def parse_text(self, text: str, **kwargs) -> PortfolioModel:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'portfolio.xml'
            path.write_text(text, encoding='utf-8')
            return PortfolioModel.from_path(path, **kwargs)

class PortfolioXmlParsingTests(TemporaryXmlMixin, unittest.TestCase):

    def test_secure_parser_rejects_doctype_and_entity(self) -> None:
        payload = '<!DOCTYPE client [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><client><version>70</version><baseCurrency>USD</baseCurrency>&xxe;</client>'
        with self.assertRaisesRegex(XmlSecurityError, 'DOCTYPE or ENTITY'):
            self.parse_text(payload)

    def test_secure_parser_rejects_malformed_xml(self) -> None:
        with self.assertRaisesRegex(PortfolioModelError, 'malformed Portfolio XML'):
            self.parse_text('<client><version>70</client>')

    def test_supported_version_boundary_is_52_through_70(self) -> None:
        for version in (52, 53, 69, 70):
            with self.subTest(version=version):
                model = self.parse_text(minimal_xml().replace('<version>70</version>', f'<version>{version}</version>'))
                self.assertEqual(version, model.version)
        for version in (51, 71):
            with self.subTest(version=version), self.assertRaises(UnsupportedVersionError):
                self.parse_text(minimal_xml().replace('<version>70</version>', f'<version>{version}</version>'))

    def test_secure_parser_enforces_size_limit(self) -> None:
        with self.assertRaisesRegex(XmlSecurityError, 'safety limit'):
            self.parse_text(minimal_xml(), max_bytes=10)

    def test_secure_parser_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target.xml'
            target.write_text(minimal_xml(), encoding='utf-8')
            link = root / 'link.xml'
            link.symlink_to(target)
            with self.assertRaisesRegex(XmlSecurityError, 'symlink'):
                PortfolioModel.from_path(link)

    def test_no_saved_filters_exposes_only_full_client(self) -> None:
        model = self.parse_text(minimal_xml())
        self.assertEqual(1, len(model.group_scopes))
        self.assertEqual('Весь портфель', model.group_scopes[0].name)

    def test_saved_filter_preserves_weight_and_reference_account_closure(self) -> None:
        body = '<securities/><accounts><account><uuid>a</uuid><name>Cash</name><currencyCode>USD</currencyCode><isRetired>false</isRetired><transactions/></account></accounts><portfolios><portfolio><uuid>p</uuid><name>Broker</name><referenceAccount reference="../../../accounts/account"/><isRetired>false</isRetired><transactions/></portfolio></portfolios><taxonomies/><dashboards/><settings><configurationSets><entry><string>client-filter-definitions</string><config-set><configurations><config><uuid>owner</uuid><name>Joint owner</name><data>p:2500</data></config></configurations></config-set></entry></configurationSets></settings>'
        xml = '<client><version>70</version><baseCurrency>USD</baseCurrency>' + body + '</client>'
        scope = self.parse_text(xml).group_scopes[1]
        self.assertEqual(2500, scope.members[0].weight_raw)
        self.assertEqual(('a',), scope.account_uuids)
        self.assertEqual(('p',), scope.portfolio_uuids)

    def test_invalid_group_members_and_weights_fail_closed(self) -> None:

        def grouped(data: str) -> str:
            return minimal_xml(f'<settings><configurationSets><entry><string>client-filter-definitions</string><config-set><configurations><config><uuid>g</uuid><name>G</name><data>{data}</data></config></configurations></config-set></entry></configurationSets></settings>')
        with self.assertRaisesRegex(PortfolioModelError, 'member not found'):
            self.parse_text(grouped('missing'))
        with self.assertRaisesRegex(PortfolioModelError, 'outside 1..10000'):
            self.parse_text(grouped('missing:0'))

class ReferenceResolutionTests(TemporaryXmlMixin, unittest.TestCase):

    def test_unresolved_reference_has_distinct_error(self) -> None:
        body = '<portfolios><portfolio reference="../missing"/></portfolios>'
        xml = '<client><version>70</version><baseCurrency>USD</baseCurrency>' + body + '</client>'
        with self.assertRaisesRegex(UnresolvedReferenceError, 'unresolved XML reference'):
            self.parse_text(xml)

    def test_cyclic_reference_has_distinct_error(self) -> None:
        body = '<securities/><accounts/><portfolios><portfolio reference="../portfolio[2]"/><portfolio reference="../portfolio"/></portfolios><taxonomies/><dashboards/>'
        xml = '<client><version>70</version><baseCurrency>USD</baseCurrency>' + body + '</client>'
        with self.assertRaisesRegex(CyclicReferenceError, 'cyclic XML reference'):
            self.parse_text(xml)

    def test_wrong_reference_type_has_distinct_error(self) -> None:
        body = '<securities><security><uuid>s</uuid><name>S</name><currencyCode>USD</currencyCode><prices/></security></securities><accounts/><portfolios><portfolio reference="../../securities/security"/></portfolios><taxonomies/><dashboards/>'
        xml = '<client><version>70</version><baseCurrency>USD</baseCurrency>' + body + '</client>'
        with self.assertRaisesRegex(ReferenceTypeError, 'reference type mismatch'):
            self.parse_text(xml)

    def test_duplicate_uuid_has_ambiguous_identity_error(self) -> None:
        security = '<security><uuid>same</uuid><name>S</name><currencyCode>USD</currencyCode><prices/></security>'
        xml = '<client><version>70</version><baseCurrency>USD</baseCurrency><securities>' + security + security + '</securities><accounts/><portfolios/><taxonomies/><dashboards/></client>'
        with self.assertRaisesRegex(AmbiguousReferenceError, 'duplicate security UUID'):
            self.parse_text(xml)

    def test_id_and_relative_references_build_equal_canonical_models(self) -> None:
        relative = self.parse_text(self.equivalent_reference_xml('relative'))
        by_id = self.parse_text(self.equivalent_reference_xml('id'))
        self.assertEqual(relative.accounts, by_id.accounts)
        self.assertEqual(relative.portfolios, by_id.portfolios)
        self.assertEqual(relative.references, by_id.references)
        self.assertEqual(relative.audit_summary(), by_id.audit_summary())
        first = json.dumps(by_id.audit_summary(), sort_keys=True, separators=(',', ':'))
        second = json.dumps(self.parse_text(self.equivalent_reference_xml('id')).audit_summary(), sort_keys=True, separators=(',', ':'))
        self.assertEqual(first, second)

    def test_id_adapter_yields_equivalent_relative_copy_without_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix='pp-id-adapter-') as temporary:
            source = Path(temporary) / 'portfolio.xml'
            source.write_text(self.equivalent_reference_xml('id'), encoding='utf-8')
            before = source.read_bytes()
            with xpath_compatible_xml(source) as converted:
                self.assertNotEqual(source, converted)
                converted_root = ET.parse(converted).getroot()
                converted_resolver = _ReferenceResolver(converted_root)
                self.assertEqual('path', converted_resolver.mode)
                self.assertTrue(converted_resolver.audit().valid)
                self.assertFalse(any(('id' in element.attrib for element in converted_root.iter())))
                adapted = PortfolioModel.from_path(converted, allowed_root=converted.parent)
            original = PortfolioModel.from_path(source, allowed_root=source.parent)
            self.assertEqual(original.accounts, adapted.accounts)
            self.assertEqual(original.portfolios, adapted.portfolios)
            self.assertEqual(before, source.read_bytes())

    def test_absolute_single_node_xpath_is_equivalent(self) -> None:
        xml = self.equivalent_reference_xml('relative').replace('../../../accounts/account', '/client[1]/accounts[1]/account[1]')
        model = self.parse_text(xml)
        self.assertEqual('a', model.portfolios['p'].reference_account_uuid)

    @staticmethod
    def equivalent_reference_xml(mode: str) -> str:
        account_id = ' id="101"' if mode == 'id' else ''
        reference = '101' if mode == 'id' else '../../../accounts/account'
        return f'<client><version>70</version><baseCurrency>USD</baseCurrency><securities/><accounts><account{account_id}><uuid>a</uuid><name>Cash</name><currencyCode>USD</currencyCode><isRetired>false</isRetired><transactions/></account></accounts><portfolios><portfolio><uuid>p</uuid><name>Broker</name><referenceAccount reference="{reference}"/><isRetired>false</isRetired><transactions/></portfolio></portfolios><taxonomies/><dashboards/></client>'

class ScalingTests(unittest.TestCase):

    def test_money_scale_is_exact_and_raw_is_retained(self) -> None:
        value = money_value('12345')
        self.assertEqual(MONEY_DIVIDER, value.divider)
        self.assertEqual(12345, value.raw)
        self.assertEqual(Decimal('123.45'), value.value)

    def test_quote_scale_is_exact_and_raw_is_retained(self) -> None:
        value = quote_value('123456789')
        self.assertEqual(QUOTE_DIVIDER, value.divider)
        self.assertEqual(123456789, value.raw)
        self.assertEqual(Decimal('1.23456789'), value.value)

    def test_share_scale_is_exact_and_raw_is_retained(self) -> None:
        value = share_value('100000001')
        self.assertEqual(SHARE_DIVIDER, value.divider)
        self.assertEqual(100000001, value.raw)
        self.assertEqual(Decimal('1.00000001'), value.value)

    def test_weight_scale_is_exact_and_raw_is_retained(self) -> None:
        value = weight_value('10000')
        self.assertEqual(WEIGHT_DIVIDER, value.divider)
        self.assertEqual(10000, value.raw)
        self.assertEqual(Decimal('100'), value.value)

    def test_rate_is_decimal_and_rejects_non_finite(self) -> None:
        self.assertEqual(Decimal('1.2345678901'), rate_value('1.2345678901'))
        with self.assertRaisesRegex(PortfolioModelError, 'not finite'):
            rate_value('NaN')
