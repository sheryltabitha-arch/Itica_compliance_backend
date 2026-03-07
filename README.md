# Clone the empty repo
git clone https://github.com/yourusername/itica-kyc-platform.git
cd itica-kyc-platform

# Initialize Python project
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Create folder structure
mkdir -p app/{db,middleware,models,routers,services,inference}
mkdir -p tests/{unit,integration}
mkdir -p docs

# Create all __init__.py files
touch app/__init__.py
touch app/db/__init__.py
touch app/middleware/__init__.py
touch app/models/__init__.py
touch app/routers/__init__.py
touch app/services/__init__.py
touch app/inference/__init__.py
touch tests/__init__.py
touch tests/unit/__init__.py
touch tests/integration/__init__.py# Itica_compliance_backend
#!/bin/bash
# Run this script to create all files

# Create directory structure
mkdir -p app/{db,middleware,models,routers,services,inference}
mkdir -p tests/{unit,integration}
mkdir -p .github/workflows
mkdir -p docs

# Create __init__.py files
touch app/__init__.py
touch app/db/__init__.py
touch app/middleware/__init__.py
touch app/models/__init__.py
touch app/routers/__init__.py
touch app/services/__init__.py
touch app/inference/__init__.py
touch tests/__init__.py
touch tests/unit/__init__.py
touch tests/integration/__init__.py

# Create root files
touch main.py
touch requirements.txt
touch docker-compose.yml
touch .env.example
touch .gitignore
touch README.md
touch AUTH0_SETUP.md
touch BACKEND_ARCHITECTURE.md

# Create app modules
touch app/middleware/auth.py
touch app/models/models.py
touch app/db/session.py
touch app/services/auth0_service.py
touch app/services/audit_ledger.py
touch app/services/document_upload.py
touch app/services/human_review.py
touch app/services/drift_monitor.py
touch app/services/fraud_detector.py

# Create routers
touch app/routers/auth.py
touch app/routers/documents.py
touch app/routers/extraction.py
touch app/routers/human_review.py
touch app/routers/reports.py
touch app/routers/health.py

# Create inference
touch app/inference/service.py
touch app/inference/classifier.py
touch app/inference/field_validator.py
touch app/inference/layoutlmv3_extractor.py
touch app/inference/fraud_detector.py

# Create tests
touch tests/test_auth.py
touch tests/test_documents.py
touch tests/test_extraction.py

echo "✓ All files created!"
