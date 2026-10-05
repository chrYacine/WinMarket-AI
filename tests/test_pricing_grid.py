"""Pricing page — current commercial grid (Starter / Business / Business MAX).

"Business MAX" is a display label only: its contact link keeps the technical plan identifier "Enterprise"
(contact form value, stored contact requests), see tests/test_saas_contact.py.
"""
from __future__ import annotations

import re

STARTER = [
    "1 utilisateur",
    "3 analyses complètes d'AO",
    "150 Mo taille max par appel d'offre",
    "Moteur d'analyse – RAG intégré",
    "Recommandation GO / GO sous réserve / NO-GO",
    "Génération de documents administratifs (DC1, DC2, DUME)",
]
BUSINESS = [
    "5 utilisateurs",
    "Jusqu'à 15 AO analysés",
    "550 Mo taille max par appel d'offre",
    "Moteur d'analyse – RAG intégré",
    "Recommandation GO / GO sous réserve / NO-GO",
    "Génération de documents administratifs (DC1, DC2, DUME)",
    "Personnalisation des livrables",
]
BUSINESS_MAX = [
    "Utilisateurs illimités / gestion des rôles",
    "Volume adapté au besoin",
    "Adapté au besoin",
    "Moteur d'analyse – RAG intégré",
    "Recommandation GO / GO sous réserve / NO-GO",
    "Génération de documents administratifs (DC1, DC2, DUME)",
    "Intégration de bases de données entreprise / API ...",
    "Accompagnement sur mesure",
]


def _cards(html: str) -> list[str]:
    grid = html.split('<div class="pricing-grid">', 1)[1].split('<p class="pricing-note">', 1)[0]
    return re.split(r'<div class="pricing-card[^"]*">', grid)[1:]


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).replace("&#39;", "'").strip()


def _features(card: str) -> list[str]:
    return [_text(label) for label in re.findall(r'<span class="pricing-spec-label">(.*?)</span>', card)]


def test_pricing_page_shows_the_three_current_offers(client):
    response = client.get("/pricing")
    assert response.status_code == 200
    starter, business, business_max = _cards(response.text)

    assert re.search(r'pricing-plan-name">Starter<', starter)
    assert re.search(r'pricing-plan-name">Business<', business)
    assert re.search(r'pricing-plan-name">Business MAX<', business_max)
    assert "Enterprise" not in _text(response.text)  # never shown to the visitor any more
    assert "Entreprise" not in _text(response.text)

    assert _text(starter.split('class="price-monthly">', 1)[1].split("</span></span>", 1)[0]) == "30 € / mois"
    for card in (business, business_max):
        assert re.search(r'class="price-monthly">Sur devis</span>', card)
        assert re.search(r'class="price-annual" hidden>Sur devis</span>', card)

    assert _features(starter) == STARTER
    assert _features(business) == BUSINESS
    assert _features(business_max) == BUSINESS_MAX
    assert "pricing-badge" in business  # the existing "Recommandée" badge stays on Business

    # Existing call-to-action links are unchanged.
    assert 'href="/register"' in starter
    assert 'href="/contact?plan=Business"' in business
    assert 'href="/contact?plan=Enterprise"' in business_max
