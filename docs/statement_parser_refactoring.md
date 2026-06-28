# Statement Parser Architecture Refactoring

## Problem Statement

Current issues with monthly statement processing:
1. **Data duplication**: 67% of Futu transactions are duplicates due to weak deduplication
2. **Missing validation**: No checks for transaction completeness or position accuracy
3. **No common interface**: Each broker has different method signatures
4. **Silent failures**: Parsing errors not detected or reported
5. **Tight coupling**: Import logic mixed with business rules

## Design Pattern: Strategy + Template Method

### 1. Common Interface (Strategy Pattern)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ParseResult:
    """Standardized parse result"""
    broker_code: str
    statement_month: str
    transactions: list[dict]
    dividends: list[dict]
    positions: list[dict]
    warnings: list[str]
    errors: list[str]
    
    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

class StatementParser(ABC):
    """Common interface for all broker statement parsers"""
    
    @abstractmethod
    def can_parse(self, pdf_path: Path) -> bool:
        """Check if this parser can handle the PDF"""
        pass
    
    @abstractmethod
    def extract_text(self, pdf_path: Path) -> str:
        """Extract and normalize text from PDF"""
        pass
    
    @abstractmethod
    def parse_transactions(self, text: str) -> list[dict]:
        """Parse transaction records"""
        pass
    
    @abstractmethod
    def parse_dividends(self, text: str) -> list[dict]:
        """Parse dividend records"""
        pass
    
    @abstractmethod
    def parse_positions(self, text: str) -> list[dict]:
        """Parse position snapshots"""
        pass
    
    def parse(self, pdf_path: Path) -> ParseResult:
        """Template method - standard parse flow"""
        text = self.extract_text(pdf_path)
        
        result = ParseResult(
            broker_code=self.broker_code,
            statement_month=self.extract_month(pdf_path),
            transactions=[],
            dividends=[],
            positions=[],
            warnings=[],
            errors=[]
        )
        
        try:
            result.transactions = self.parse_transactions(text)
        except Exception as e:
            result.errors.append(f"Transaction parse failed: {e}")
        
        try:
            result.dividends = self.parse_dividends(text)
        except Exception as e:
            result.errors.append(f"Dividend parse failed: {e}")
        
        try:
            result.positions = self.parse_positions(text)
        except Exception as e:
            result.errors.append(f"Position parse failed: {e}")
        
        return result
```

### 2. Harness Validation Rules

Add to `src/harness/statement_validation.py`:

```python
@dataclass
class StatementValidationResult:
    passed: bool
    completeness_score: float  # 0-100%
    accuracy_score: float      # 0-100%
    issues: list[str]

class StatementValidator:
    """Validate statement parsing quality"""
    
    def validate_completeness(self, result: ParseResult) -> StatementValidationResult:
        """ST-001: Check for missing data"""
        issues = []
        
        # Check if all months are present
        # Check if position section exists
        # Check if transaction count matches expected
        
        return StatementValidationResult(...)
    
    def validate_accuracy(self, result: ParseResult) -> StatementValidationResult:
        """ST-002: Check data correctness"""
        issues = []
        
        # Check for duplicate transactions
        # Check for missing dates
        # Check for impossible values (negative qty, etc)
        # Cross-validate: positions should match cumulative transactions
        
        return StatementValidationResult(...)
    
    def validate_positions(self, result: ParseResult, prev_positions: list) -> StatementValidationResult:
        """ST-003: Position reconciliation"""
        # Current positions = Previous + Buys - Sells
        # Allow small differences due to timing
        
        return StatementValidationResult(...)
```

### 3. Deduplication Service

```python
class StatementDeduplicator:
    """Prevent duplicate imports"""
    
    def should_import(self, pdf_path: Path, broker_code: str) -> tuple[bool, str]:
        """Check if statement should be imported"""
        file_hash = self._compute_hash(pdf_path)
        month = self._extract_month(pdf_path)
        
        # Check by hash
        existing = self.db.get_by_hash(file_hash)
        if existing:
            return False, f"Already imported as statement_id={existing['id']}"
        
        # Check by month+broker (prevent re-import)
        existing = self.db.get_by_month(broker_code, month)
        if existing:
            return False, f"Month {month} already imported as statement_id={existing['id']}"
        
        return True, "OK"
```

### 4. Implementation Plan

**Phase 1: Data Cleanup (Immediate)**
1. Delete duplicate statement_files (keep latest per month)
2. Delete orphaned transactions (no valid statement_file_id)
3. Re-import with proper deduplication

**Phase 2: Refactoring (1-2 days)**
1. Create `StatementParser` base class
2. Refactor FutuImporter to inherit from base
3. Refactor LongbridgeImporter to inherit from base
4. Refactor BOCIImporter to inherit from base
5. Add `StatementDeduplicator` service

**Phase 3: Validation Harness (1 day)**
1. Create `StatementValidator` class
2. Implement ST-001 (completeness)
3. Implement ST-002 (accuracy)
4. Implement ST-003 (position reconciliation)
5. Integrate into import flow

**Phase 4: Monitoring (Ongoing)**
1. Add metrics: parse success rate, duplicate detection rate
2. Alert on validation failures
3. Dashboard for data quality

## Expected Outcomes

After refactoring:
- **Zero duplicates**: Deduplication prevents re-import
- **100% validation**: All imports pass harness checks
- **Consistent interface**: All brokers follow same pattern
- **Early error detection**: Parsing failures caught immediately
- **Audit trail**: Clear record of what was imported and when

## Files to Create/Modify

**New files:**
- `src/parsers/base.py` - StatementParser interface
- `src/parsers/futu_parser.py` - Futu implementation
- `src/parsers/longbridge_parser.py` - Longbridge implementation
- `src/parsers/boci_parser.py` - BOCI implementation
- `src/harness/statement_validation.py` - Validation rules
- `src/services/deduplicator.py` - Deduplication service

**Modified files:**
- `src/database/import_statements.py` - Use new parsers
- `src/harness/quality.py` - Integrate statement validation
- `src/cli.py` - Add validation commands

## Success Criteria

- [ ] All Futu positions correctly extracted
- [ ] Zero duplicate transactions
- [ ] All imports pass validation harness
- [ ] Parse errors reported, not silently ignored
- [ ] Position reconciliation within 1% tolerance
