# Salesforce CPQ Configuration Extractor & Client Document Generator

A local-first, AWS-ready pipeline that extracts active Salesforce CPQ (`SBQQ__`) configuration, normalizes relational structures into streaming NDJSON and a 21-tab Excel workbook, and transforms complex technical setup into an executive, client-shareable Word document (`.docx`).

---

## Discovered CPQ Schema & Field Names (from Sample Org Inspection)

Inspection of the real extracted data (`cpq_nested_extract.json` / `sample_extract.json`) revealed the exact field names and relational shapes across active CPQ configuration objects:

### 1. Product2 (186 Active Products)
- **Key Populated Fields (49 of 93)**:
  - Identification: `Id`, `Name`, `ProductCode`, `Family`, `Description`, `IsActive`, `IsArchived`, `Category__c`, `Extras__c`
  - Bundling & Configuration: `SBQQ__ConfigurationType__c`, `SBQQ__ConfigurationEvent__c`, `SBQQ__OptionLayout__c`, `SBQQ__OptionSelectionMethod__c`, `SBQQ__CustomConfigurationRequired__c`, `SBQQ__HasConfigurationAttributes__c`, `SBQQ__Component__c`, `SBQQ__Optional__c`
  - Pricing & Subscriptions: `SBQQ__PricingMethod__c`, `SBQQ__PricingMethodEditable__c`, `SBQQ__CostEditable__c`, `SBQQ__PriceEditable__c`, `SBQQ__BlockPricingField__c`, `SBQQ__SubscriptionPricing__c`, `SBQQ__SubscriptionTerm__c`, `SBQQ__SubscriptionType__c`, `SBQQ__SubscriptionBase__c`, `SBQQ__SubscriptionPercent__c`
  - Billing & Assets: `SBQQ__BillingFrequency__c`, `SBQQ__BillingType__c`, `SBQQ__ChargeType__c`, `SBQQ__AssetAmendmentBehavior__c`, `SBQQ__AssetConversion__c`
  - Catalog Breakdown by Family:
    - `Hardware`: 88
    - `Miscellaneous`: 37
    - `Service`: 14
    - `Software`: 9
    - `Consumable`: 9
    - `Support`: 5
    - Unassigned (`None`): 24
  - Bundles vs Standalone: 13 products configured as bundles (>=1 option), 173 standalone products.

### 2. SBQQ__ProductRule__c & Children (6 Active Rules)
- **Product Rule Fields**:
  - `Id`, `Name`, `SBQQ__Active__c`
  - Rule Type: `SBQQ__Type__c` (`Validation`, `Selection`, `Alert`, `Filter`)
  - Scope: `SBQQ__Scope__c` (`Product`, `Quote`)
  - Evaluation: `SBQQ__EvaluationEvent__c` (`Always`, `Save`, `Edit`)
  - Conditions Met: `SBQQ__ConditionsMet__c` (`All`, `Any`, `Custom`)
  - Logic & Messaging: `SBQQ__AdvancedCondition__c` (e.g. `1 AND 2`), `SBQQ__ErrorMessage__c`
- **SBQQ__ErrorCondition__c (8 Conditions)**:
  - `Id`, `Name`, `SBQQ__Rule__c`
  - Tested Target: `SBQQ__TestedObject__c` (e.g. `Configuration Attributes`), `SBQQ__TestedField__c`, `SBQQ__TestedVariable__c` (foreign key to `SBQQ__SummaryVariable__c`)
  - Comparison: `SBQQ__Operator__c` (`equals`, `greater than`, `less than`), `SBQQ__FilterType__c`, `SBQQ__FilterValue__c`, `SBQQ__FilterVariable__c`
  - Indexing: `SBQQ__Index__c`
- **SBQQ__ProductAction__c (4 Actions)**:
  - `Id`, `Name`, `SBQQ__Rule__c`, `SBQQ__Type__c` (`Add`, `Remove`, `Enable`, `Disable`, `Show`, `Hide`)
  - Target: `SBQQ__Product__c` (FK to Product2), `SBQQ__Required__c`, `SBQQ__Value__c`
- **SBQQ__ConfigurationRule__c (6 Records)**:
  - `Id`, `Name`, `SBQQ__Active__c`, `SBQQ__Product__c`, `SBQQ__ProductRule__c`, `SBQQ__ProductFeature__c`, `SBQQ__RuleEvaluationEvent__c`, `SBQQ__RuleType__c`

### 3. SBQQ__PriceRule__c & Children (1 Active Rule)
- **Price Rule Fields**:
  - `Id`, `Name`, `SBQQ__Active__c`, `SBQQ__TargetObject__c` (`Configurator`, `Calculator`), `SBQQ__EvaluationEvent__c` (`On Calculate`, `Before Calculate`, `After Calculate`), `SBQQ__ConditionsMet__c`, `SBQQ__Product__c` (product scope, if any)
- **Price Conditions & Actions**:
  - `SBQQ__PriceCondition__c`: `SBQQ__Rule__c`, `SBQQ__Field__c`, `SBQQ__Operator__c`, `SBQQ__Value__c`
  - `SBQQ__PriceAction__c`: `SBQQ__Rule__c`, `SBQQ__Field__c`, `SBQQ__TargetObject__c`, `SBQQ__Value__c`

### 4. SBQQ__SummaryVariable__c (4 Records)
- `Id`, `Name`, `SBQQ__TargetObject__c` (`Product Option`), `SBQQ__AggregateFunction__c` (`Sum`, `Count`, `Average`), `SBQQ__AggregateField__c` (`Quantity`), `SBQQ__FilterField__c` (`Product Code`), `SBQQ__Operator__c` (`equals`), `SBQQ__FilterValue__c`, `SBQQ__Scope__c` (`Quote`)

### 5. Features, Options, & Pricing Objects
- `SBQQ__ProductFeature__c` (33 records): `Id`, `Name`, `SBQQ__ConfiguredSKU__c`, `SBQQ__Number__c`, `SBQQ__MinOptions__c`, `SBQQ__MaxOptions__c`, `SBQQ__Category__c`
- `SBQQ__ProductOption__c` (98 records): `Id`, `SBQQ__ConfiguredSKU__c`, `SBQQ__OptionalSKU__c`, `SBQQ__Feature__c`, `SBQQ__Number__c`, `SBQQ__Quantity__c`, `SBQQ__Selected__c`, `SBQQ__Required__c`, `SBQQ__Type__c`, `SBQQ__UnitPrice__c`
- `PricebookEntry` (184 records across 1 standard pricebook): `Id`, `Product2Id`, `UnitPrice`, `IsActive` (2 products have no price book entry)
- `SBQQ__Cost__c` (92 records): `Id`, `SBQQ__Product__c`, `SBQQ__UnitCost__c`, `SBQQ__Active__c`
- `SBQQ__BlockPrice__c` (8 records): `Id`, `SBQQ__Product__c`, `SBQQ__LowerBound__c`, `SBQQ__UpperBound__c`, `SBQQ__Price__c`
- `SBQQ__Dimension__c` (2 records): `Id`, `SBQQ__Product__c`, `SBQQ__UnitPrice__c`, `SBQQ__Type__c`
- `SBQQ__OptionConstraint__c` (1 record): `Id`, `SBQQ__ConfiguredSKU__c`, `SBQQ__ConstrainedOption__c`, `SBQQ__ConstrainingOption__c`, `SBQQ__Type__c` (`Dependency`)
- `SBQQ__ConfigurationAttribute__c` (12 records): `Id`, `SBQQ__Product__c`, `Name`, `SBQQ__TargetField__c`, `SBQQ__Position__c`, `SBQQ__ShownValues__c`

---

## Client Document Generation (`doc_gen/`)

The `doc_gen/` package turns the raw technical extraction into a non-technical, client-ready Word document (`.docx`).

### Architectural Principle
- **Code = Facts**: All numbers, statistics, counts, and catalog anomalies are deterministically computed in Python (`facts.py`). The LLM never counts, invents IDs, or calculates metrics.
- **LLM = Plain English Wording**: The LLM translates small, pre-processed packets with 100% resolved names into clear business descriptions for executives and business users.
- **Strict Guardrails**:
  - No Salesforce IDs or `SBQQ__` API names in the document body (enforced via regex validation).
  - Exact prices and cost records are excluded from LLM input by default (`LLM_INCLUDE_PRICES=false`).
  - Numbers in synthesis use placeholders (`{{active_products}}`) substituted directly from `facts.json`.
  - Offline testing supported via `--mock-llm` and `--dry-run`.

### Document Structure (9 Sections)
1. **Executive Summary**: High-level overview of the sales setup, catalog scale, and key focus areas.
2. **How to Read This Document**: Plain-English glossary for non-Salesforce stakeholders.
3. **Your Catalog at a Glance**: Summary facts, products by family table, and rules breakdown.
4. **How Products Are Bundled and Configured**: Plain-language descriptions of bundles and buyer options.
5. **Business Rules in Plain English**: Product validation, auto-addition, and pricing rules explained cleanly.
6. **How Pricing Works**: High-level pricing models (block pricing, recurring dimensions, subscription terms).
7. **Things Worth a Closer Look**: Objective observations (bundles without features, unpriced products, empty rule actions).
8. **Questions to Confirm with Your Team**: Clarifications for sales and operations teams.
9. **Appendix**: Technical cross-reference (Salesforce object names, files, and mapping notes).

---

## CLI Usage

### Running Document Generation
```bash
python run_document.py --run data/runs/sample_org/20261015_150908 --client-name "Acme Corp" --mock-llm
```

### Dry Run (Token & Call Estimation)
```bash
python run_document.py --run data/runs/sample_org/20261015_150908 --dry-run
```

### Render Only (Edit Text & Re-render without LLM Calls)
```bash
python run_document.py --run data/runs/sample_org/20261015_150908 --render-only
```