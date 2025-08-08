from flask import Flask, render_template, request, flash, redirect, url_for
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import hashlib
import os

app = Flask(__name__)
app.secret_key = 'rrc14042003ritabrataroychoudhuryadmin'  # Use a different secret key

# Initialize Firebase (with error handling)
try:
    cred = credentials.Certificate('firebase_config.json')
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Firebase initialized successfully!")
except Exception as e:
    print(f"Firebase initialization error: {e}")
    db = None

def hash_password(password):
    """Hash password using SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()

def username_exists(username):
    """Check if username already exists"""
    try:
        users_ref = db.collection("users")
        query = users_ref.where("username", "==", username).limit(1)
        docs = list(query.stream())
        return len(docs) > 0
    except Exception as e:
        print(f"Error checking username: {e}")
        return False

def email_exists(email):
    """Check if email already exists"""
    try:
        users_ref = db.collection("users")
        query = users_ref.where("email", "==", email).limit(1)
        docs = list(query.stream())
        return len(docs) > 0
    except Exception as e:
        print(f"Error checking email: {e}")
        return False

@app.route('/')
def home():
    """Home page for admin registration"""
    if db is None:
        flash('Database connection error. Please check Firebase configuration.', 'error')
    return render_template('admin_register.html')

@app.route('/register_admin', methods=['GET', 'POST'])
def register_admin():
    """Admin registration"""
    if db is None:
        flash('Database connection error. Please check Firebase configuration.', 'error')
        return render_template('admin_register.html')
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        print(f"Received form data: name={name}, email={email}, username={username}")
        
        # Validation
        if not all([name, email, phone, username, password, confirm_password]):
            flash('All fields are required.', 'error')
            print("Validation failed: Missing fields")
            return render_template('admin_register.html')
        
        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            print("Validation failed: Passwords don't match")
            return render_template('admin_register.html')
        
        if len(password) < 8:
            flash('Password must be at least 8 characters long.', 'error')
            print("Validation failed: Password too short")
            return render_template('admin_register.html')
        
        if username_exists(username):
            flash('Username already exists. Please choose a different one.', 'error')
            print(f"Validation failed: Username {username} exists")
            return render_template('admin_register.html')
        
        if email_exists(email):
            flash('Email already exists. Please use a different email.', 'error')
            print(f"Validation failed: Email {email} exists")
            return render_template('admin_register.html')
        
        # Create admin user
        try:
            admin_data = {
                "name": name,
                "email": email,
                "phone": phone,
                "username": username,
                "role": "admin",  # Always admin
                "password": hash_password(password)
            }
            
            print(f"Creating admin with data: {admin_data}")
            
            # Add document to Firestore
            doc_ref = db.collection('users').document()
            doc_ref.set(admin_data)
            
            print(f"Admin user created successfully with ID: {doc_ref.id}")
            flash(f'Admin user "{username}" created successfully!', 'success')
            return redirect(url_for('success'))
            
        except Exception as e:
            print(f"Database error: {e}")
            flash(f'Admin registration failed: {str(e)}', 'error')
            return render_template('admin_register.html')
    
    return render_template('admin_register.html')

@app.route('/success')
def success():
    """Success page"""
    return render_template('success.html')

@app.route('/list_admins')
def list_admins():
    """List all admin users"""
    if db is None:
        flash('Database connection error. Please check Firebase configuration.', 'error')
        return redirect(url_for('home'))
    
    try:
        users_ref = db.collection("users")
        query = users_ref.where("role", "==", "admin")
        docs = query.stream()
        
        admins = []
        for doc in docs:
            admin_data = doc.to_dict()
            admin_data['doc_id'] = doc.id
            # Don't show password
            admin_data.pop('password', None)
            admins.append(admin_data)
        
        print(f"Found {len(admins)} admin users")
        return render_template('list_admins.html', admins=admins)
        
    except Exception as e:
        print(f"Error fetching admin users: {e}")
        flash(f'Error fetching admin users: {str(e)}', 'error')
        return redirect(url_for('home'))

@app.route('/test_db')
def test_db():
    """Test database connection"""
    if db is None:
        return "Database not initialized!"
    
    try:
        # Try to read from database
        users_ref = db.collection("users")
        docs = list(users_ref.limit(1).stream())
        return f"Database connection successful! Found {len(docs)} document(s) in users collection."
    except Exception as e:
        return f"Database connection error: {e}"

if __name__ == '__main__':
    print("Starting admin registration app...")
    print(f"Firebase config exists: {os.path.exists('firebase_config.json')}")
    app.run(debug=True, port=5001)  # Use different port to avoid conflict