from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import hashlib
import os
from functools import wraps
from datetime import datetime, timezone
import uuid
from werkzeug.utils import secure_filename
import base64
from PIL import Image
import io

app = Flask(__name__)
app.secret_key = 'rrc14042003ritabrataroychoudhury'  # Change this to a secure random key

# Initialize Firebase (removed storage bucket since we're not using it)
cred = credentials.Certificate('firebase_config.json')
firebase_admin.initialize_app(cred)
db = firestore.client()

# Allowed file extensions for image upload
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def hash_password(password):
    """Hash password using SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()

def username_exists(username):
    """Check if username already exists"""
    users_ref = db.collection("users")
    query = users_ref.where("username", "==", username).limit(1)
    docs = list(query.stream())
    return len(docs) > 0

def get_user_by_username(username):
    """Get user data by username"""
    users_ref = db.collection("users")
    query = users_ref.where("username", "==", username).limit(1)
    docs = query.stream()
    
    for doc in docs:
        user_data = doc.to_dict()
        user_data['doc_id'] = doc.id
        return user_data
    return None

def update_user_points(user_id, points_to_add=10):
    """Update user points by adding specified amount"""
    try:
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        
        if user_doc.exists:
            current_points = user_doc.to_dict().get('points', 0)
            new_points = current_points + points_to_add
            
            user_ref.update({
                'points': new_points,
                'last_points_update': datetime.now()
            })
            
            return new_points
        return None
    except Exception as e:
        print(f"Error updating points: {str(e)}")
        return None

def login_required(f):
    """Decorator to require login for certain routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator to require admin role"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        
        user = get_user_by_username(session['username'])
        if not user or user.get('role') != 'admin':
            flash('Admin access required.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def safe_datetime_sort_key(record):
    """Safe sorting key function that handles timezone-aware and naive datetimes"""
    created_at = record.get('created_at')
    if created_at is None:
        # Return a very old timezone-aware datetime as default
        return datetime.min.replace(tzinfo=timezone.utc)
    
    # If datetime is naive, make it timezone-aware
    if created_at.tzinfo is None:
        return created_at.replace(tzinfo=timezone.utc)
    
    return created_at

def compress_and_convert_to_base64(file, max_size_kb=800, quality=85):
    """
    Compress image and convert to base64 string
    Args:
        file: Uploaded file object
        max_size_kb: Maximum size in KB (default 800KB to stay under 1MB Firestore limit)
        quality: JPEG quality (1-100, default 85)
    """
    try:
        # Read the image
        image = Image.open(file)
        
        # Convert to RGB if necessary (for JPEG compatibility)
        if image.mode in ('RGBA', 'P'):
            image = image.convert('RGB')
        
        # Get original format
        original_format = image.format or 'JPEG'
        
        # Resize if image is too large (optional)
        max_dimension = 1920  # Max width or height
        if max(image.size) > max_dimension:
            ratio = max_dimension / max(image.size)
            new_size = tuple(int(dim * ratio) for dim in image.size)
            image = image.resize(new_size, Image.Resampling.LANCZOS)
        
        # Save to bytes with compression
        output = io.BytesIO()
        
        # Use JPEG for better compression
        if original_format.upper() in ['PNG', 'GIF'] and quality < 95:
            save_format = 'JPEG'
            mime_type = 'image/jpeg'
        else:
            save_format = original_format
            mime_type = f'image/{original_format.lower()}'
        
        # Try different quality levels if image is too large
        current_quality = quality
        while current_quality > 20:
            output.seek(0)
            output.truncate(0)
            
            if save_format.upper() == 'JPEG':
                image.save(output, format=save_format, quality=current_quality, optimize=True)
            else:
                image.save(output, format=save_format, optimize=True)
            
            # Check size
            size_kb = len(output.getvalue()) / 1024
            if size_kb <= max_size_kb:
                break
            
            current_quality -= 10
        
        # Convert to base64
        output.seek(0)
        image_data = output.getvalue()
        base64_string = base64.b64encode(image_data).decode('utf-8')
        
        # Create data URL
        data_url = f"data:{mime_type};base64,{base64_string}"
        
        # Log compression info
        final_size_kb = len(base64_string) / 1024 * 3/4  # Base64 is ~33% larger
        print(f"Image compressed: {final_size_kb:.1f}KB, Quality: {current_quality}%")
        
        return data_url, len(base64_string)
        
    except Exception as e:
        print(f"Error processing image: {str(e)}")
        return None, 0

def store_large_image_in_chunks(base64_data, document_id, collection_name="image_chunks"):
    """
    Store large base64 image in chunks if it exceeds Firestore document size limit
    """
    try:
        chunk_size = 900000  # ~900KB per chunk to stay under 1MB limit
        chunks = []
        
        # Split base64 data into chunks
        for i in range(0, len(base64_data), chunk_size):
            chunks.append(base64_data[i:i + chunk_size])
        
        # Store chunks
        batch = db.batch()
        
        for i, chunk in enumerate(chunks):
            chunk_ref = db.collection(collection_name).document(f"{document_id}_chunk_{i}")
            batch.set(chunk_ref, {
                "chunk_data": chunk,
                "chunk_index": i,
                "parent_id": document_id,
                "created_at": datetime.now()
            })
        
        # Store metadata
        meta_ref = db.collection(collection_name).document(f"{document_id}_meta")
        batch.set(meta_ref, {
            "total_chunks": len(chunks),
            "parent_id": document_id,
            "created_at": datetime.now()
        })
        
        batch.commit()
        return True
        
    except Exception as e:
        print(f"Error storing image chunks: {str(e)}")
        return False

def retrieve_chunked_image(document_id, collection_name="image_chunks"):
    """
    Retrieve and reconstruct chunked image
    """
    try:
        # Get metadata
        meta_ref = db.collection(collection_name).document(f"{document_id}_meta")
        meta_doc = meta_ref.get()
        
        if not meta_doc.exists:
            return None
        
        total_chunks = meta_doc.to_dict()["total_chunks"]
        
        # Retrieve all chunks
        reconstructed_data = ""
        for i in range(total_chunks):
            chunk_ref = db.collection(collection_name).document(f"{document_id}_chunk_{i}")
            chunk_doc = chunk_ref.get()
            
            if chunk_doc.exists:
                reconstructed_data += chunk_doc.to_dict()["chunk_data"]
        
        return reconstructed_data
        
    except Exception as e:
        print(f"Error retrieving image chunks: {str(e)}")
        return None

@app.route('/')
def home():
    """Home page - redirect based on user role if logged in"""
    if 'user_id' in session:
        user = get_user_by_username(session['username'])
        if user and user.get('role') == 'admin':
            return redirect(url_for('admin_panel'))
        else:
            return redirect(url_for('dashboard'))
    return render_template('home.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration - only creates regular users"""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        # Validation
        if not all([name, email, phone, username, password]):
            flash('All fields are required.', 'error')
            return render_template('register.html')
        
        if username_exists(username):
            flash('Username already exists. Please choose a different one.', 'error')
            return render_template('register.html')
        
        # Create user with 'user' role only and initialize points
        try:
            user_data = {
                "name": name,
                "email": email,
                "phone": phone,
                "username": username,
                "role": "user",  # Always 'user' for public registration
                "password": hash_password(password),
                "points": 0,  # Initialize points to 0
                "created_at": datetime.now()
            }
            
            record = db.collection('users').document()
            record.set(user_data)
            
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
            
        except Exception as e:
            flash(f'Registration failed: {str(e)}', 'error')
            return render_template('register.html')
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login - redirects based on user role"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('Username and password are required.', 'error')
            return render_template('login.html')
        
        user = get_user_by_username(username)
        
        if user and user['password'] == hash_password(password):
            session['user_id'] = user['doc_id']
            session['username'] = user['username']
            session['role'] = user['role']
            flash(f'Welcome back, {user["name"]}!', 'success')
            
            # Redirect based on user role
            if user['role'] == 'admin':
                return redirect(url_for('admin_panel'))
            else:
                return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password.', 'error')
    
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    """User dashboard - for regular users only"""
    user = get_user_by_username(session['username'])
    
    # Redirect admin users to admin panel
    if user and user.get('role') == 'admin':
        return redirect(url_for('admin_panel'))
    
    # Get user's records - simple query without ordering to avoid index requirement
    records_ref = db.collection("records")
    query = records_ref.where("user_id", "==", session['user_id'])
    user_records = []
    
    for doc in query.stream():
        record_data = doc.to_dict()
        record_data['doc_id'] = doc.id
        
        # Process location data for display
        if 'Location' in record_data and record_data['Location']:
            location = record_data['Location']
            record_data['latitude'] = location.latitude
            record_data['longitude'] = location.longitude
            record_data['location_display'] = f"{location.latitude:.6f}, {location.longitude:.6f}"
        else:
            record_data['latitude'] = None
            record_data['longitude'] = None
            record_data['location_display'] = "No location available"
        
        # Handle image display
        if record_data.get('photo_chunked') and record_data.get('photo_id'):
            record_data['has_image'] = True
            record_data['image_url'] = url_for('get_image', record_id=doc.id)
        elif record_data.get('photo'):
            record_data['has_image'] = True
            record_data['image_url'] = None  # Image is directly in the document
        else:
            record_data['has_image'] = False
            record_data['image_url'] = None
        
        user_records.append(record_data)
    
    # Sort in Python instead of Firestore using safe datetime function
    user_records.sort(key=safe_datetime_sort_key, reverse=True)
    
    # Get user's current points
    user_points = user.get('points', 0)
    complaints_count = len(user_records)
    
    return render_template('dashboard.html', user=user, complaints=user_records, 
                         user_points=user_points, complaints_count=complaints_count)

@app.route('/submit_complaint', methods=['GET', 'POST'])
@login_required
def submit_complaint():
    """Submit a new complaint with photo and location - stores image as Base64"""
    user = get_user_by_username(session['username'])
    
    # Redirect admin users to admin panel
    if user and user.get('role') == 'admin':
        return redirect(url_for('admin_panel'))
    
    if request.method == 'POST':
        description = request.form.get('description', '').strip()
        latitude = request.form.get('latitude')
        longitude = request.form.get('longitude')
        photo = request.files.get('photo')
        complaint_type = request.form.get('type', 'Traffic Violation')
        number_plate = request.form.get('numberPlate', '').strip()
        
        # Validation
        if not description:
            flash('Description is required.', 'error')
            return render_template('submit_complaint.html')
        
        if not photo or photo.filename == '':
            flash('Photo is required.', 'error')
            return render_template('submit_complaint.html')
        
        if not allowed_file(photo.filename):
            flash('Invalid file type. Please upload PNG, JPG, JPEG, or GIF files only.', 'error')
            return render_template('submit_complaint.html')
        
        try:
            # Reset file pointer
            photo.seek(0)
            
            # Compress and convert image to base64
            photo_base64, base64_size = compress_and_convert_to_base64(photo)
            
            if not photo_base64:
                flash('Failed to process image. Please try again.', 'error')
                return render_template('submit_complaint.html')
            
            # Generate unique document ID
            record_id = str(uuid.uuid4())
            
            # Create record data
            record_data = {
                "Name": user['name'],
                "Phone": int(user['phone']) if user['phone'].isdigit() else user['phone'],
                "type": complaint_type,
                "numberPlate": number_plate,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                
                # Create GeoPoint for location
                "Location": firestore.GeoPoint(
                    float(latitude) if latitude else 0.0,
                    float(longitude) if longitude else 0.0
                ) if latitude and longitude else None,
                
                # Additional fields for internal tracking
                "user_id": session['user_id'],
                "username": session['username'],
                "description": description,
                "created_at": datetime.now(),
                "status": "pending"
            }
            
            # Check if image is too large for single document (>800KB base64 ≈ 600KB actual)
            if base64_size > 800000:  # If base64 string is > 800KB
                # Store image in chunks
                success = store_large_image_in_chunks(photo_base64, record_id)
                if success:
                    record_data["photo_chunked"] = True
                    record_data["photo_id"] = record_id
                    flash('Large image stored successfully!', 'info')
                else:
                    flash('Failed to store large image. Please try again.', 'error')
                    return render_template('submit_complaint.html')
            else:
                # Store image directly in document
                record_data["photo"] = photo_base64
                record_data["photo_chunked"] = False
            
            # Remove None values
            record_data = {k: v for k, v in record_data.items() if v is not None}
            
            # Save to "records" collection
            record = db.collection('records').document(record_id)
            record.set(record_data)
            
            # Award points to user (10 points per complaint)
            if user.get('role') == 'user':  # Only award points to regular users, not admins
                new_points = update_user_points(session['user_id'], 10)
                if new_points is not None:
                    flash(f'Complaint submitted successfully! You earned 10 points. Total points: {new_points}', 'success')
                else:
                    flash('Complaint submitted successfully! (Points update failed)', 'success')
            else:
                flash('Complaint submitted successfully!', 'success')
            
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            flash(f'Failed to submit complaint: {str(e)}', 'error')
            print(f"Error details: {str(e)}")
            return render_template('submit_complaint.html')
    
    return render_template('submit_complaint.html')

@app.route('/admin')
@admin_required
def admin_panel():
    """Admin panel - for admin users only"""
    # Get all users
    users_ref = db.collection("users")
    docs = users_ref.stream()
    
    users = []
    for doc in docs:
        user_data = doc.to_dict()
        user_data['doc_id'] = doc.id
        # Don't send password to frontend
        user_data.pop('password', None)
        # Ensure points field exists for display
        user_data['points'] = user_data.get('points', 0)
        users.append(user_data)
    
    # Sort users by points (descending) for leaderboard effect
    users.sort(key=lambda x: x.get('points', 0), reverse=True)
    
    # Get all records from "records" collection
    records_ref = db.collection("records")
    record_docs = records_ref.stream()
    
    records = []
    for doc in record_docs:
        record_data = doc.to_dict()
        record_data['doc_id'] = doc.id
        
        # Process location data for display
        if 'Location' in record_data and record_data['Location']:
            location = record_data['Location']
            record_data['latitude'] = location.latitude
            record_data['longitude'] = location.longitude
            record_data['location_display'] = f"{location.latitude:.6f}, {location.longitude:.6f}"
        else:
            record_data['latitude'] = None
            record_data['longitude'] = None
            record_data['location_display'] = "No location available"
        
        # Handle chunked images for display
        if record_data.get('photo_chunked') and record_data.get('photo_id'):
            record_data['has_image'] = True
            record_data['image_url'] = url_for('get_image', record_id=doc.id)
            # Show placeholder for performance in admin panel
            record_data['photo'] = "data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjAwIiBoZWlnaHQ9IjEwMCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMTAwJSIgaGVpZ2h0PSIxMDAlIiBmaWxsPSIjZGRkIi8+PHRleHQgeD0iNTAlIiB5PSI1MCUiIGZvbnQtZmFtaWx5PSJBcmlhbCIgZm9udC1zaXplPSIxNCIgZmlsbD0iIzk5OSIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZHk9Ii4zZW0iPkxhcmdlIEltYWdlPC90ZXh0Pjwvc3ZnPg=="
        elif record_data.get('photo'):
            record_data['has_image'] = True
            record_data['image_url'] = None  # Image is directly in the document
        else:
            record_data['has_image'] = False
            record_data['image_url'] = None
        
        records.append(record_data)
    
    # Sort in Python instead of Firestore using safe datetime function  
    records.sort(key=safe_datetime_sort_key, reverse=True)
    
    # Calculate statistics
    total_users = len(users)
    total_complaints = len(records)
    total_points_awarded = sum(user.get('points', 0) for user in users if user.get('role') == 'user')
    
    return render_template('admin.html', users=users, complaints=records,
                         total_users=total_users, total_complaints=total_complaints,
                         total_points_awarded=total_points_awarded)

@app.route('/complaint/<complaint_id>')
@login_required
def view_complaint(complaint_id):
    """View detailed complaint information"""
    try:
        # Get the complaint record
        record_ref = db.collection('records').document(complaint_id)
        record_doc = record_ref.get()
        
        if not record_doc.exists:
            flash('Complaint not found.', 'error')
            user = get_user_by_username(session['username'])
            if user and user.get('role') == 'admin':
                return redirect(url_for('admin_panel'))
            else:
                return redirect(url_for('dashboard'))
        
        complaint = record_doc.to_dict()
        complaint['doc_id'] = complaint_id
        
        # Check if user has permission to view this complaint
        user = get_user_by_username(session['username'])
        if user.get('role') != 'admin' and complaint.get('user_id') != session['user_id']:
            flash('Access denied.', 'error')
            return redirect(url_for('dashboard'))
        
        # Process location data
        if 'Location' in complaint and complaint['Location']:
            location = complaint['Location']
            complaint['latitude'] = location.latitude
            complaint['longitude'] = location.longitude
            complaint['location_display'] = f"{location.latitude:.6f}, {location.longitude:.6f}"
        else:
            complaint['latitude'] = None
            complaint['longitude'] = None
            complaint['location_display'] = "No location available"
        
        # Handle image data
        if complaint.get('photo_chunked') and complaint.get('photo_id'):
            # Load chunked image for detailed view
            photo_data = retrieve_chunked_image(complaint.get('photo_id', complaint_id))
            complaint['photo'] = photo_data
            complaint['has_image'] = bool(photo_data)
        elif complaint.get('photo'):
            complaint['has_image'] = True
        else:
            complaint['has_image'] = False
        
        # Format datetime for display
        if 'created_at' in complaint:
            created_at = complaint['created_at']
            if hasattr(created_at, 'strftime'):
                complaint['created_at_display'] = created_at.strftime("%Y-%m-%d %H:%M:%S")
            else:
                complaint['created_at_display'] = str(created_at)
        
        if 'updated_at' in complaint:
            updated_at = complaint['updated_at']
            if hasattr(updated_at, 'strftime'):
                complaint['updated_at_display'] = updated_at.strftime("%Y-%m-%d %H:%M:%S")
            else:
                complaint['updated_at_display'] = str(updated_at)
        
        # Check if template exists, if not use a simple response
        try:
            return render_template('complaint_detail.html', complaint=complaint, user=user)
        except:
            # Fallback: return complaint data as JSON if template doesn't exist
            return jsonify({
                'success': True,
                'complaint': complaint,
                'message': 'Template not found, returning JSON data'
            })
        
    except Exception as e:
        print(f"Error in view_complaint: {str(e)}")
        flash(f'Error loading complaint: {str(e)}', 'error')
        user = get_user_by_username(session['username'])
        if user and user.get('role') == 'admin':
            return redirect(url_for('admin_panel'))
        else:
            return redirect(url_for('dashboard'))

@app.route('/get_image/<record_id>')
@login_required
def get_image(record_id):
    """Get image for a specific record (handles both direct and chunked storage)"""
    try:
        # Get record
        record_ref = db.collection('records').document(record_id)
        record_doc = record_ref.get()
        
        if not record_doc.exists:
            return jsonify({'error': 'Record not found'}), 404
        
        record_data = record_doc.to_dict()
        
        # Check if user has permission to view this record
        user = get_user_by_username(session['username'])
        if user.get('role') != 'admin' and record_data.get('user_id') != session['user_id']:
            return jsonify({'error': 'Access denied'}), 403
        
        # Get image data
        if record_data.get('photo_chunked'):
            # Retrieve chunked image
            photo_data = retrieve_chunked_image(record_data.get('photo_id', record_id))
        else:
            # Get direct image
            photo_data = record_data.get('photo')
        
        if photo_data:
            return jsonify({'photo': photo_data})
        else:
            return jsonify({'error': 'Image not found'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/create_user', methods=['GET', 'POST'])
@admin_required
def create_admin_user():
    """Create admin users - only accessible by admins"""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user')
        
        # Validation
        if not all([name, email, phone, username, password]):
            flash('All fields are required.', 'error')
            return render_template('create_user.html')
        
        if username_exists(username):
            flash('Username already exists. Please choose a different one.', 'error')
            return render_template('create_user.html')
        
        # Create user with points initialization
        try:
            user_data = {
                "name": name,
                "email": email,
                "phone": phone,
                "username": username,
                "role": role,
                "password": hash_password(password),
                "points": 0 if role == 'user' else None,  # Only users get points
                "created_at": datetime.now()
            }
            
            record = db.collection('users').document()
            record.set(user_data)
            
            flash(f'{role.title()} user created successfully!', 'success')
            return redirect(url_for('admin_panel'))
            
        except Exception as e:
            flash(f'User creation failed: {str(e)}', 'error')
            return render_template('create_user.html')
    
    return render_template('create_user.html')

@app.route('/admin/update_complaint_status', methods=['POST'])
@admin_required
def update_complaint_status():
    """Update complaint status"""
    complaint_id = request.form.get('complaint_id')
    new_status = request.form.get('status')
    
    try:
        # Update in "records" collection
        record_ref = db.collection('records').document(complaint_id)
        record_ref.update({
            'status': new_status,
            'updated_at': datetime.now()
        })
        flash('Complaint status updated successfully!', 'success')
    except Exception as e:
        flash(f'Failed to update status: {str(e)}', 'error')
    
    return redirect(url_for('admin_panel'))

@app.route('/leaderboard')
@login_required
def leaderboard():
    """Display user leaderboard based on points"""
    try:
        # Get all users with points
        users_ref = db.collection("users")
        query = users_ref.where("role", "==", "user")  # Only regular users
        
        leaderboard_users = []
        for doc in query.stream():
            user_data = doc.to_dict()
            user_data['doc_id'] = doc.id
            # Don't send password to frontend
            user_data.pop('password', None)
            # Ensure points field exists
            user_data['points'] = user_data.get('points', 0)
            
            # Get complaint count for each user
            user_complaints = db.collection("records").where("user_id", "==", doc.id).stream()
            user_data['complaint_count'] = len(list(user_complaints))
            
            leaderboard_users.append(user_data)
        
        # Sort by points (descending)
        leaderboard_users.sort(key=lambda x: x.get('points', 0), reverse=True)
        
        # Add rank
        for i, user in enumerate(leaderboard_users):
            user['rank'] = i + 1
        
        current_user = get_user_by_username(session['username'])
        
        return render_template('leaderboard.html', users=leaderboard_users, current_user=current_user)
        
    except Exception as e:
        flash(f'Error loading leaderboard: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    """User logout"""
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('home'))

@app.route('/profile')
@login_required
def profile():
    """User profile"""
    user = get_user_by_username(session['username'])
    
    # Get user's complaint count
    if user:
        user_complaints = db.collection("records").where("user_id", "==", user['doc_id']).stream()
        complaint_count = len(list(user_complaints))
        user['complaint_count'] = complaint_count
        user['points'] = user.get('points', 0)
    
    return render_template('profile.html', user=user)

@app.route('/api/records')
@login_required
def api_records():
    """API endpoint to get records (useful for AJAX calls)"""
    try:
        records_ref = db.collection("records")
        
        # If user is admin, get all records
        user = get_user_by_username(session['username'])
        if user and user.get('role') == 'admin':
            query = records_ref
        else:
            # If regular user, get only their records
            query = records_ref.where("user_id", "==", session['user_id'])
        
        records = []
        for doc in query.stream():
            record_data = doc.to_dict()
            record_data['doc_id'] = doc.id
            
            # Convert datetime to string for JSON serialization
            if 'created_at' in record_data:
                created_at = record_data['created_at']
                if hasattr(created_at, 'isoformat'):
                    record_data['created_at'] = created_at.isoformat()
                else:
                    record_data['created_at'] = str(created_at)
            
            if 'updated_at' in record_data:
                updated_at = record_data['updated_at']
                if hasattr(updated_at, 'isoformat'):
                    record_data['updated_at'] = updated_at.isoformat()
                else:
                    record_data['updated_at'] = str(updated_at)
            
            # Convert GeoPoint to lat/lng for JSON
            if 'Location' in record_data and record_data['Location']:
                location = record_data['Location']
                record_data['Location'] = {
                    'latitude': location.latitude,
                    'longitude': location.longitude
                }
            
            # Handle chunked images in API response
            if record_data.get('photo_chunked'):
                record_data['photo_url'] = url_for('get_image', record_id=doc.id)
            
            records.append(record_data)
        
        # Sort records using safe datetime function
        records.sort(key=lambda x: safe_datetime_sort_key({'created_at': x.get('created_at')}), reverse=True)
        
        return jsonify({
            'success': True,
            'records': records
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

if __name__ == '__main__':
    app.run(debug=True)