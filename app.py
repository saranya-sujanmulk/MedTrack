from flask import Flask, render_template, request, redirect, session, url_for, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import boto3

# --------------------------------------
# Load environment variables
# --------------------------------------
load_dotenv()

# Flask setup
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)

# --------------------------------------
# DynamoDB + SNS setup
# --------------------------------------
AWS_REGION_NAME = os.environ.get('AWS_REGION_NAME', 'us-east-1')

try:
    # only secret key is set, so credentials will be picked from environment or IAM role if on EC2
    dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION_NAME)
    sns = boto3.client('sns', region_name=AWS_REGION_NAME)
    SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", None)
except Exception as e:
    print(f"AWS services init failed, falling back to local: {e}")
    dynamodb = None
    sns = None
    SNS_TOPIC_ARN = None

# --------------------------------------
# Table Names
# --------------------------------------
USERS_TABLE_NAME = 'MedtrackUsers'
APPOINTMENTS_TABLE_NAME = 'MedtrackAppointments'
PRESCRIPTIONS_TABLE_NAME = 'MedtrackPrescriptions'

# --------------------------------------
# Local fallback
# --------------------------------------
local_users = {}
appointments = []
prescriptions = []

# --------------------------------------
# SNS helper
# --------------------------------------
def publish_notification(message, subject="MedtrackNotifications"):
    if sns and SNS_TOPIC_ARN:
        try:
            sns.publish(
                TopicArn=SNS_TOPIC_ARN,
                Message=message,
                Subject=subject
            )
        except Exception as e:
            print(f"Error publishing to SNS: {e}")
    else:
        print("SNS not configured, skipping notification.")

# --------------------------------------
# Load current user
# --------------------------------------
@app.before_request
def load_logged_in_user():
    user_email = session.get('user_email')
    g.user = None
    if user_email:
        if dynamodb:
            table = dynamodb.Table(USERS_TABLE_NAME)
            try:
                resp = table.get_item(Key={'email': user_email})
                g.user = resp.get('Item')
            except Exception as e:
                print(f"Error fetching user from DynamoDB: {e}")
        else:
            g.user = local_users.get(user_email)

# --------------------------------------
# Routes
# --------------------------------------

@app.route('/')
def first():
    return render_template('first.html')

# --------------------------------------
# Signup
# --------------------------------------
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        confirm = request.form.get('confirm')
        role = request.form.get('role')
        age = request.form.get('age')
        gender = request.form.get('gender')
        specialty = request.form.get('specialty')
        location = request.form.get('location')
        license_no = request.form.get('license')

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template('signup.html')

        hashed_password = generate_password_hash(password)
        user_data = {
            'email': email,
            'name': name,
            'password': hashed_password,
            'role': role.lower(),
            'created_at': datetime.now().isoformat()
        }

        if role.lower() == 'patient':
            user_data.update({'age': age, 'gender': gender})
        elif role.lower() == 'doctor':
            user_data.update({'specialty': specialty, 'location': location, 'license_no': license_no})

        if dynamodb:
            table = dynamodb.Table(USERS_TABLE_NAME)
            try:
                table.put_item(Item=user_data)
                publish_notification(
                    f"New {role} account created: {name} ({email})",
                    "Medtrack New Signup"
                )
            except Exception as e:
                print(f"Error writing user to DynamoDB: {e}")
        else:
            local_users[email] = user_data

        session['user_email'] = email
        return redirect(url_for('doctor_dashboard' if role.lower() == 'doctor' else 'patient_dashboard'))

    return render_template('signup.html')

# --------------------------------------
# Login
# --------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        user = None

        if dynamodb:
            table = dynamodb.Table(USERS_TABLE_NAME)
            try:
                resp = table.get_item(Key={'email': email})
                user = resp.get('Item')
            except Exception as e:
                print(f"Error fetching user from DynamoDB: {e}")
        else:
            user = local_users.get(email)

        if user and check_password_hash(user['password'], password):
            session['user_email'] = email
            return redirect(url_for('doctor_dashboard' if user['role'] == 'doctor' else 'patient_dashboard'))
        else:
            flash("Invalid email or password.", "error")
            return redirect(url_for('login'))

    return render_template('login.html')

# --------------------------------------
# Logout
# --------------------------------------
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('first'))

# --------------------------------------
# Doctor Dashboard
# --------------------------------------
@app.route('/doctor_dashboard')
def doctor_dashboard():
    if not g.user or g.user['role'] != 'doctor':
        flash("Unauthorized", "error")
        return redirect(url_for('login'))

    doctor_name = f"Dr. {g.user['name']} ({g.user.get('specialty','')})"

    if dynamodb:
        try:
            table = dynamodb.Table(APPOINTMENTS_TABLE_NAME)
            response = table.scan()
            doctor_appts = [a for a in response.get('Items', []) if a['doctor_name'] == doctor_name]
        except Exception as e:
            print(f"Error fetching appointments: {e}")
            doctor_appts = []
    else:
        doctor_appts = [a for a in appointments if a['doctor_name'] == doctor_name]

    doctor_presc = [p for p in prescriptions if p['doctor_email'] == g.user['email']]

    return render_template('doctor_dashboard.html', user=g.user, appointments=doctor_appts, prescriptions=doctor_presc)

# --------------------------------------
# Patient Dashboard
# --------------------------------------
@app.route('/patient_dashboard')
def patient_dashboard():
    if not g.user or g.user['role'] != 'patient':
        flash("Unauthorized", "error")
        return redirect(url_for('login'))

    user_appts = []
    user_presc = []
    all_doctors = []

    if dynamodb:
        try:
            appt_table = dynamodb.Table(APPOINTMENTS_TABLE_NAME)
            appt_resp = appt_table.scan()
            user_appts = [a for a in appt_resp.get('Items', []) if a['patient_email'] == g.user['email']]

            presc_table = dynamodb.Table(PRESCRIPTIONS_TABLE_NAME)
            presc_resp = presc_table.scan()
            user_presc = [p for p in presc_resp.get('Items', []) if p['patient_email'] == g.user['email']]

            users_table = dynamodb.Table(USERS_TABLE_NAME)
            users_resp = users_table.scan()
            all_doctors = [
                f"Dr. {u['name']} ({u.get('specialty', 'N/A')})"
                for u in users_resp.get('Items', [])
                if u['role'] == 'doctor'
            ]
        except Exception as e:
            print(f"Error loading patient dashboard data: {e}")
    else:
        user_appts = [a for a in appointments if a['patient_email'] == g.user['email']]
        user_presc = [p for p in prescriptions if p['patient_email'] == g.user['email']]
        all_doctors = [
            f"Dr. {user['name']} ({user['specialty']})"
            for user in local_users.values()
            if user['role'] == 'doctor'
        ]

    return render_template(
        'patient_dashboard.html',
        user=g.user,
        appointments=user_appts,
        prescriptions=user_presc,
        doctors=all_doctors
    )


# --------------------------------------
# Book appointment
# --------------------------------------
@app.route('/book_appointment', methods=['POST'])
def book_appointment():
    if not g.user or g.user['role'] != 'patient':
        flash("Unauthorized", "error")
        return redirect(url_for('login'))

    doctor = request.form.get('doctor')
    date = request.form.get('date')
    time = request.form.get('time')
    reason = request.form.get('reason')

    new_appt = {
        'appointment_id': str(len(appointments) + 1),
        'doctor_name': doctor,
        'date': date,
        'time': time,
        'reason': reason,
        'status': 'Pending',
        'patient_email': g.user['email'],
        'patient_name': g.user['name']
    }

    if dynamodb:
        table = dynamodb.Table(APPOINTMENTS_TABLE_NAME)
        try:
            table.put_item(Item=new_appt)
            publish_notification(
                f"New appointment booked with {doctor} on {date} at {time}",
                "Medtrack New Appointment"
            )
        except Exception as e:
            print(f"Error saving appointment: {e}")
    else:
        appointments.append(new_appt)

    flash("Appointment booked successfully.", "success")
    return redirect(url_for('patient_dashboard'))

# --------------------------------------
# Doctor update appointment status
# --------------------------------------
@app.route('/doctor/update_status', methods=['POST'])
def update_appointment_status():
    if not g.user or g.user['role'] != 'doctor':
        flash("Unauthorized", "error")
        return redirect(url_for('login'))

    appt_id = request.form.get('appointment_id')
    new_status = request.form.get('status')

    for appt in appointments:
        if appt['appointment_id'] == appt_id:
            appt['status'] = new_status
            break

    flash("Appointment status updated.", "success")
    return redirect(url_for('doctor_dashboard'))

# --------------------------------------
# Doctor issues prescription
# --------------------------------------
@app.route('/doctor/issue_prescription', methods=['POST'])
def issue_prescription():
    if not g.user or g.user['role'] != 'doctor':
        flash("Unauthorized", "error")
        return redirect(url_for('login'))

    presc = {
        'patient_email': request.form.get('patient_email'),
        'diagnosis': request.form.get('diagnosis'),
        'medications': request.form.get('meds'),
        'date_issued': datetime.now().strftime('%Y-%m-%d'),
        'doctor_email': g.user['email'],
        'doctor_name': g.user['name']
    }

    if dynamodb:
        table = dynamodb.Table(PRESCRIPTIONS_TABLE_NAME)
        try:
            table.put_item(Item=presc)
            publish_notification(
                f"Prescription issued by {g.user['name']} for {presc['patient_email']}",
                "Medtrack Prescription"
            )
        except Exception as e:
            print(f"Error saving prescription: {e}")
    else:
        prescriptions.append(presc)

    flash("Prescription submitted.", "success")
    return redirect(url_for('doctor_dashboard'))

# --------------------------------------
# About & Contact
# --------------------------------------
@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

if __name__ == '__main__':
   app.run(host='0.0.0.0', port=5000, debug=True)
