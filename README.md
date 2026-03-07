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
