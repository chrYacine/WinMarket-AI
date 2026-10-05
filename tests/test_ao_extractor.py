"""
Test suite for AO Extraction Agent
Tests the ao_extractor module for correctness and edge cases
"""

import pytest
from src.agents.ao_extractor import (
    read_document,
    _find_budget,
    _extract_technologies,
    _extract_certifications,
)
from src.core.models import AOContext
from pathlib import Path
import tempfile


class TestBudgetExtraction:
    """Tests for budget extraction logic"""
    
    def test_budget_standard_format(self):
        """Test extraction of standard budget format with € symbol"""
        text = "Le budget estimé est 250000 €"
        result = _find_budget(text)
        assert result == 250000.0
    
    def test_budget_with_thousand_separator(self):
        """Test budget with space or dot thousand separator"""
        text = "Budget: 1.500.000 €"
        result = _find_budget(text)
        assert result == 1500000.0
    
    def test_budget_with_space_separator(self):
        """Test budget with space thousand separator"""
        text = "Montant: 500 000 €"
        result = _find_budget(text)
        assert result == 500000.0
    
    def test_budget_not_found(self):
        """Test when no budget is present"""
        text = "Pas de budget mentionné dans ce document"
        result = _find_budget(text)
        assert result is None
    
    def test_budget_multiple_candidates(self):
        """Test extraction when multiple € amounts present (takes first)"""
        text = "Budget range: 200000 € to 500000 €"
        result = _find_budget(text)
        # Should return the first match
        assert result == 200000.0
    
    def test_budget_very_large(self):
        """Test extraction of very large amounts"""
        text = "Enveloppe: 10.000.000 €"
        result = _find_budget(text)
        assert result == 10000000.0
    
    def test_budget_edge_case_no_euro_symbol(self):
        """Test that budget without € symbol is not extracted"""
        text = "Budget: 500000"
        result = _find_budget(text)
        assert result is None


class TestTechnologiesExtraction:
    """Tests for technology extraction"""
    
    def test_single_technology(self):
        """Test extraction of single technology"""
        text = "Stack: Python + PostgreSQL"
        result = _extract_technologies(text)
        assert "Python" in result
        assert "PostgreSQL" in result
    
    def test_multiple_technologies(self):
        """Test extraction of multiple technologies"""
        text = "Required: React, Angular, Vue, Node.js, AWS, Docker"
        result = _extract_technologies(text)
        assert "React" in result
        assert "Angular" in result
        assert "AWS" in result
        assert "Docker" in result
    
    def test_case_insensitive(self):
        """Test that extraction is case-insensitive"""
        text = "We use PYTHON, java, and REACT"
        result = _extract_technologies(text)
        assert "Python" in result
        assert "Java" in result
        assert "React" in result
    
    def test_no_false_positives(self):
        """Test that common words are not extracted as tech"""
        text = "Nous utilisons des technologies avancées"
        result = _extract_technologies(text)
        # Should not include generic words
        assert "technologies" not in result
        assert "avancées" not in result
    
    def test_technology_not_in_vocab(self):
        """Test unknown technology is not extracted"""
        text = "We will use CustomTechXYZ framework"
        result = _extract_technologies(text)
        assert "CustomTechXYZ" not in result
    
    def test_alphabetical_order(self):
        """Test that results are alphabetically sorted"""
        text = "Stack: React, Python, AWS, Docker, Angular"
        result = _extract_technologies(text)
        assert result == sorted(result)


class TestCertificationExtraction:
    """Tests for certification extraction (critical!)"""
    
    def test_iso27001_mandatory(self):
        """Test extraction of mandatory ISO 27001"""
        text = "ISO 27001 certification est obligatoire"
        result = _extract_certifications(text)
        assert "ISO 27001" in result
    
    def test_secnumcloud_mandatory(self):
        """Test extraction of mandatory SecNumCloud"""
        text = "Qualification SecNumCloud est requise"
        result = _extract_certifications(text)
        assert "SecNumCloud" in result
    
    def test_certification_not_mandatory_excluded(self):
        """Test that non-mandatory certifications are excluded"""
        text = "ISO 27001 est apprécié mais non obligatoire"
        result = _extract_certifications(text)
        assert "ISO 27001" not in result
    
    def test_certification_souhaitee_excluded(self):
        """Test that 'souhaité' (wanted) certifications are excluded"""
        text = "Une certification ISO 9001 serait souhaitée mais non requise"
        result = _extract_certifications(text)
        assert "ISO 9001" not in result
    
    def test_multiple_mandatory_certs(self):
        """Test extraction of multiple mandatory certifications"""
        text = """
        Les certifications obligatoires sont:
        - ISO 27001: requis
        - RGPD compliance: obligatoire
        - SecNumCloud: exigé
        """
        result = _extract_certifications(text)
        assert "ISO 27001" in result
        assert "RGPD" in result
        assert "SecNumCloud" in result
    
    def test_hds_mandatory(self):
        """Test extraction of HDS certification"""
        text = "Hébergeur de données de santé certification est obligatoire"
        result = _extract_certifications(text)
        assert "HDS" in result
    
    def test_negation_window_detection(self):
        """Test that negation in proximity is detected"""
        text = "SecNumCloud certification n'est pas requise pour ce projet"
        result = _extract_certifications(text)
        assert "SecNumCloud" not in result
    
    def test_qualiopi_mandatory(self):
        """Test extraction of Qualiopi certification"""
        text = "Certification Qualiopi est obligatoire"
        result = _extract_certifications(text)
        assert "Qualiopi" in result
    
    def test_mixed_mandatory_and_optional(self):
        """Test extraction with mix of mandatory and optional"""
        text = """
        SecNumCloud: obligatoire
        ISO 9001: apprécié mais pas obligatoire
        ISO 27001: requis
        Qualiopi: souhaité
        """
        result = _extract_certifications(text)
        assert "SecNumCloud" in result
        assert "ISO 27001" in result
        assert "ISO 9001" not in result
        assert "Qualiopi" not in result


class TestDocumentReading:
    """Tests for document reading from various formats"""
    
    def test_read_txt_file(self):
        """Test reading plain text file"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("Test content")
            f.flush()
            result = read_document(f.name)
            assert "Test content" in result
    
    def test_read_markdown_file(self):
        """Test reading markdown file"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("# Header\nContent here")
            f.flush()
            result = read_document(f.name)
            assert "Header" in result
            assert "Content here" in result
    
    def test_empty_document(self):
        """Test handling of empty document"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("")
            f.flush()
            result = read_document(f.name)
            assert result == ""


class TestIntegration:
    """Integration tests with realistic AO examples"""
    
    def test_realistic_ao_extraction(self):
        """Test extraction on realistic AO text"""
        sample_ao = """
        APPEL D'OFFRES - Transformation Digitale SAP
        
        Client: Mutuelle Horizon Protection
        Secteur: Assurance
        
        Budget estimé: 450.000 €
        
        Deadline réponse: 15 juin 2026
        
        Durée projet: 12 mois
        
        Technologies demandées:
        - SAP S/4HANA
        - Python pour développements customs
        - Docker et Kubernetes
        - AWS pour infrastructure
        
        Certifications obligatoires:
        - SecNumCloud est requise (données sensibles)
        - ISO 27001 est obligatoire
        - RGPD compliance nécessaire
        
        Certifications appréciées mais non obligatoires:
        - Qualiopi (souhaité mais non requis)
        
        Compétences requises:
        - Expert SAP (5+ years)
        - Architecture Cloud
        - DevOps
        """
        
        budget = _find_budget(sample_ao)
        techs = _extract_technologies(sample_ao)
        certs = _extract_certifications(sample_ao)
        
        # Validate extraction
        assert budget == 450000.0
        assert "SAP" in techs or "Python" in techs or "AWS" in techs
        assert "SecNumCloud" in certs
        assert "ISO 27001" in certs
        assert "RGPD" in certs
        assert "Qualiopi" not in certs  # Should be excluded


class TestEdgeCases:
    """Edge case and robustness tests"""
    
    def test_unicode_characters(self):
        """Test handling of accented characters"""
        text = "Budget: 250.000 € - Certifications: RGPD, ISO 27001"
        budget = _find_budget(text)
        assert budget == 250000.0
    
    def test_malformed_text(self):
        """Test extraction with malformed/messy text"""
        messy_text = "buget  250 000  €  ??  techs:  PYTHON  ,  AWS  ,"
        budget = _find_budget(messy_text)
        techs = _extract_technologies(messy_text)
        # Should not crash, may extract partial results
        assert isinstance(budget, (float, type(None)))
        assert isinstance(techs, list)
    
    def test_very_long_document(self):
        """Test extraction from very long document"""
        long_text = "irrelevant text " * 10000
        long_text += "Budget: 100000 € - SecNumCloud obligatoire"
        
        budget = _find_budget(long_text)
        certs = _extract_certifications(long_text)
        
        assert budget == 100000.0
        assert "SecNumCloud" in certs


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
