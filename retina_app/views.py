import os
from django.shortcuts import render, redirect
from django.db import models
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from .models import UserProfile, MedicalReport
from django.http import JsonResponse
from .ml_utils import predict_image, predict_from_features
import re
from .pdf_utils import generate_pdf_report
from django.core.files.base import ContentFile
from datetime import datetime
import json
from django.utils.timesince import timesince
from django.utils import timezone
from datetime import timedelta

def index(request):
    return render(request, 'index.html')

def login_view(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        # In this project, we use email as username
        user = authenticate(request, username=email, password=password)
        
        if user is not None:
            login(request, user)
            try:
                profile = user.userprofile
                if profile.role == 'doctor':
                    return redirect('doctor')
                elif profile.role == 'lab':
                    return redirect('lab')
                elif profile.role == 'admin':
                    return redirect('admin_panel')
            except UserProfile.DoesNotExist:
                # Fallback if no profile exists
                return redirect('index')
        else:
            messages.error(request, "Invalid email or password")
            
    return render(request, 'login.html')

def logout_view(request):
    logout(request)
    return redirect('login')

@login_required
def doctor_view(request):
    if request.user.userprofile.role != 'doctor':
        return redirect('index')
    
    # Fetch reports for this doctor (or all if simplified logic)
    reports = MedicalReport.objects.all().order_by('-created_at')
    
    # Serialize for JS
    patients_list = []
    for r in reports:
        # Determine status
        status = r.status

        # Use stored risk factor or fallback to defaults
        r_label = "N/A"
        r_val = r.risk_factor or 0
        
        if r.prediction:
            if 'high' in r.prediction.lower():
                r_label = "High Risk"
                if r_val == 0: r_val = 88 # Fallback for old records
            elif 'low' in r.prediction.lower():
                r_label = "Low Risk"
                if r_val == 0: r_val = 12 # Fallback for old records

        patients_list.append({
            'name': r.patient_name,
            'id': r.patient_id,
            'status': status,
            'updated': f"{timesince(r.created_at)} ago",
            'risk': r_val,
            'risk_label': r_label,
            'pdf_url': r.pdf_report.url if r.pdf_report else ""
        })
    
    context = {
        'reports': reports,
        'patients': patients_list,
        'patient_count': reports.count(),
        'high_risk_count': reports.filter(prediction__icontains='high').count(),
        'completed_count': reports.filter(status='Completed').count(),
        'pending_count': reports.filter(status='In Progress').count() + reports.filter(status='Pending').count(),
    }
    return render(request, 'doctor.html', context)

@login_required
def lab_view(request):
    if request.user.userprofile.role != 'lab':
        return redirect('index')
    
    reports = MedicalReport.objects.all().order_by('-created_at')
    
    # Separate lists for 'Registry' (Pending) and 'Uploads' (Completed with image)
    # Using simple logic: if image is present, it's a "sync", else it's a "pending patient"
    
    patients_list = []
    uploads_list = []
    
    for r in reports:
        # Determine status
        status = r.status
        
        # Add to registry list
        doc_display = "Unassigned"
        if r.doctor:
            if r.doctor.first_name or r.doctor.last_name:
                doc_display = f"Dr. {r.doctor.first_name} {r.doctor.last_name}".strip()
            else:
                doc_display = r.doctor.username

        # Use stored risk factor
        r_val = r.risk_factor or 0
        r_label = "Low Risk" # Default
        
        pred_clean = str(r.prediction or "").lower()
        if 'high' in pred_clean:
            r_label = "High Risk"
            if r_val == 0: r_val = 88
        elif 'low' in pred_clean:
            r_label = "Low Risk"
            if r_val == 0: r_val = 12
        else:
            r_label = "N/A"
            r_val = 0

        patients_list.append({
            'name': r.patient_name,
            'pid': r.patient_id,
            'doctor': doc_display,
            'doctor_username': r.doctor.username if r.doctor else "",
            'status': status,
            'risk': r_val,
            'risk_label': r_label,
            'pdf_url': r.pdf_report.url if r.pdf_report else "",
            'created': f"{timesince(r.created_at)} ago"
        })

        # Add to uploads list if it has an image
        if r.image:
             # Use stored risk factor
             u_label = "N/A"
             u_risk_val = r.risk_factor or 0
             if r.prediction:
                 p_lower = r.prediction.lower()
                 if 'high' in p_lower:
                     u_label = "High Risk"
                     if u_risk_val == 0: u_risk_val = 88
                 elif 'low' in p_lower:
                     u_label = "Low Risk"
                     if u_risk_val == 0: u_risk_val = 12

             uploads_list.append({
                 'patient': r.patient_name,
                 'file': os.path.basename(r.image.name),
                 'type': 'Retina Scan',
                 'risk': u_risk_val,
                 'pdf_url': r.pdf_report.url if r.pdf_report else "",
                 'when': f"{timesince(r.created_at)} ago",
                 'prediction': u_label
             })
             
    # Fetch doctors
    doctor_users = User.objects.filter(userprofile__role='doctor')
    doctors_list = []
    for d in doctor_users:
        display_name = f"Dr. {d.first_name} {d.last_name}" if (d.first_name or d.last_name) else d.username
        doctors_list.append({
            'username': d.username,
            'display_name': display_name
        })
        
    context = {
        'patients': patients_list,
        'uploads': uploads_list,
        'doctors': doctors_list,
        'total_count': reports.count(),
        'pending_count': reports.filter(image='').count()
    }

    return render(request, 'lab.html', context)

@login_required
def admin_view(request):
    if request.user.userprofile.role != 'admin':
        return redirect('index')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_user':
            name = request.POST.get('name')
            email = request.POST.get('email')
            password = request.POST.get('password')
            role = request.POST.get('role')
            specialization = request.POST.get('specialization')
            
            if User.objects.filter(username=email).exists():
                messages.error(request, f"User with email {email} already exists.")
            else:
                user = User.objects.create_user(username=email, email=email, password=password, first_name=name)
                UserProfile.objects.create(user=user, role=role, specialization=specialization)
                messages.success(request, f"Successfully added {role}: {name}")
                return redirect('admin_panel')
        
        elif action == 'onboard_patient':
            pname = request.POST.get('patient_name')
            pid = request.POST.get('patient_id')
            pdoc_username = request.POST.get('doctor_username')
            
            if pname and pid:
                pid = pid.strip()
                # Block if:
                # 1. Any COMPLETED report exists for this ID in the last 7 days
                # 2. OR any PENDING/IN PROGRESS report exists right now (to prevent duplicates)
                one_week_ago = timezone.now() - timedelta(days=7)
                active_or_recent = MedicalReport.objects.filter(patient_id__iexact=pid).filter(
                    models.Q(created_at__gte=one_week_ago, prediction__isnull=False) | 
                    models.Q(prediction__isnull=True) | 
                    models.Q(prediction='')
                ).exists()

                if active_or_recent:
                    messages.error(request, f"Protocol ID '{pid}' is already active or was submitted recently (within 7 days).")
                    return redirect('admin_panel')

                try:
                    report = MedicalReport(patient_name=pname, patient_id=pid)
                    if pdoc_username:
                        try:
                            doc = User.objects.get(username=pdoc_username, userprofile__role='doctor')
                            report.doctor = doc
                        except User.DoesNotExist:
                            pass
                    report.save()
                    messages.success(request, f"Successfully onboarded patient: {pname}")
                except Exception as e:
                    messages.error(request, f"Error: Protocol ID '{pid}' already exists or invalid data.")
            else:
                messages.error(request, "Missing patient name or ID.")
            return redirect('admin_panel')

    # Get user lists
    doctors = User.objects.filter(userprofile__role='doctor')
    labs = User.objects.filter(userprofile__role='lab')
    
    # Fetch doctors for onboarding dropdown
    doctor_users = User.objects.filter(userprofile__role='doctor')
    doctors_list_dropdown = []
    for d in doctor_users:
        name = f"Dr. {d.first_name} {d.last_name}" if d.first_name else d.username
        doctors_list_dropdown.append({'username': d.username, 'display_name': name})

    context = {
        'doctors': doctors,
        'labs': labs,
        'doctor_count': doctors.count(),
        'lab_count': labs.count(),
        'total_users': User.objects.count(),
        'doctors_list_dropdown': doctors_list_dropdown,
        'total_reports': MedicalReport.objects.count(),
        'high_risk_percentage': round((MedicalReport.objects.filter(prediction__icontains='high').count() / MedicalReport.objects.count() * 100), 1) if MedicalReport.objects.exists() else 0
    }
    
    return render(request, 'admin.html', context)


@login_required
def analyze_image(request):
    if request.method == 'POST':
        try:
            image_file = request.FILES.get('image')
            pdf_file = request.FILES.get('pdf') # Accept manual PDF upload
            
            if not image_file or not pdf_file:
                 return JsonResponse({'status': 'error', 'message': 'Both a Retina Scan (image) and Lab Data (PDF) are required to sync.'}, status=400)

            patient_name = request.POST.get('patient_name', 'Unknown')
            patient_id = request.POST.get('patient_id', 'N/A')
            doctor_name = request.POST.get('doctor_name', 'Unassigned')

            # Check 7-day restriction ONLY for completed reports (those with a prediction)
            # Check restriction: Block if a COMPLETED report exists for this ID in the last 7 days
            if patient_id and patient_id != 'N/A':
                patient_id = patient_id.strip()
                one_week_ago = timezone.now() - timedelta(days=7)
                if MedicalReport.objects.filter(patient_id__iexact=patient_id, created_at__gte=one_week_ago).exclude(prediction__isnull=True).exclude(prediction='').exists():
                     return JsonResponse({'status': 'error', 'message': 'This patient has already completed a submission recently. Please try again after 7 days.'})
            
            # 2. Find or Create Report (MANDATORY)
            report = None
            if patient_id and patient_id != 'N/A':
                report = MedicalReport.objects.filter(patient_id=patient_id, prediction__isnull=True).last()
            
            if not report:
                report = MedicalReport(patient_name=patient_name, patient_id=patient_id)
            else:
                # Update name if a new one was provided in the upload form
                report.patient_name = patient_name

            # 3. Process Assets
            img_pred_label = "Low Risk"
            img_risk_val = 12.0
            if image_file:
                report.image = image_file
                img_pred_label, img_risk_val = predict_image(image_file)
                
                # Check for validation failure
                if isinstance(img_pred_label, str) and img_pred_label.startswith("INVALID:"):
                    error_msg = img_pred_label.replace("INVALID:", "").strip()
                    return JsonResponse({'status': 'error', 'message': error_msg})
            
            # PDF Analysis & Data Extraction
            pdf_pred = None
            extracted_features = None
            if pdf_file:
                report.pdf_report = pdf_file
                found_count = 0
                try:
                    pdf_file.seek(0)
                    from pypdf import PdfReader
                    pdf_reader = PdfReader(pdf_file)
                    extracted_text = ""
                    for page in pdf_reader.pages:
                        text = page.extract_text()
                        if text:
                            extracted_text += text + " "
                    
                    text_found = len(extracted_text.strip()) > 10
                    pdf_content = extracted_text.lower().encode('utf-8', errors='ignore')

                    # --- FEATURE EXTRACTION FROM PDF ---
                    # We extract the 13 clinical markers required by the model
                    feature_patterns = {
                        'age': r'Age\s+(\d+)',
                        'sex': r'Sex\s+(\d+)',
                        'cp': r'Chest Pain Type\s*\(cp\)\s+(\d+)',
                        'trestbps': r'Resting Blood Pressure\s*\(bp\)\s+(\d+)',
                        'chol': r'Cholesterol\s*\(chol\)\s+(\d+)',
                        'fbs': r'Fasting Blood Sugar\s*\(fbs\)\s+(\d+)',
                        'restecg': r'EKG Results\s*\(ekg\)\s+(\d+)',
                        'thalach': r'Maximum Heart Rate\s*\(max_hr\)\s+(\d+)',
                        'exang': r'Exercise-Induced Angina\s*\(ex_ang\)\s+(\d+)',
                        'oldpeak': r'ST Depression\s*\(st_depr\)\s+([\d.]+)',
                        'slope': r'Slope of ST Segment\s*\(slope\)\s+(\d+)',
                        'ca': r'Number of Major Vessels\s*\(vessels\)\s+(\d+)',
                        'thal': r'Thallium Level\s*\(thal\)\s+(\d+)'
                    }
                    
                    features = []
                    feature_order = ['age', 'sex', 'cp', 'trestbps', 'chol', 'fbs', 'restecg', 'thalach', 'exang', 'oldpeak', 'slope', 'ca', 'thal']
                    
                    for key in feature_order:
                        match = re.search(feature_patterns[key], extracted_text, re.IGNORECASE)
                        if match:
                            features.append(float(match.group(1)))
                            found_count += 1
                        else:
                            # Default values or handling missing data
                            features.append(0.0) 

                    if found_count >= 10: # If we found most features, we use them
                        extracted_features = features
                        pdf_pred_label, pdf_risk_val = predict_from_features(extracted_features)
                        if pdf_pred_label:
                            pdf_pred = pdf_pred_label
                            pdf_risk = pdf_risk_val

                except Exception as e:
                    print(f"PDF Extraction Error: {e}")
                    text_found = False
                    pdf_content = b""
                finally:
                    pdf_file.seek(0)
                
                # Clinical Marker Logic
                # Specific markers from the user's PDF form
                form_markers = [b"heart disease prediction", b"data upload form", b"field name", b"expected value"]
                
                h_clinical = [b"risk: high", b"high risk", b"positive result", b"finding: abnormal", b"target: 1", b"disease: present"]
                l_clinical = [b"risk: low", b"low risk", b"negative result", b"finding: normal", b"healthy", b"clear", b"target: 0", b"disease: absent"]
                
                # Excel Data / General Medical Validation Markers (Heart Disease Dataset cols, etc.)
                valid_excel_markers = [
                    b"chol", b"thalach", b"max_hr", b"ex_ang", b"st_depr", 
                    b"fbs", b"ekg", b"blood pressure", b"vessels", 
                    b"heart", b"disease", b"chest pain", b"resting", b"angina", b"thallium",
                    b"medical", b"report", b"patient", b"clinical", b"diagnostic", b"laboratory",
                    b"serum", b"glucose", b"electrocardiographic",
                    b"(cp)", b"(bp)", b"(chol)", b"(fbs)", b"(ekg)", b"(max_hr)", b"(ex_ang)", b"(st_depr)", b"(slope)", b"(vessels)", b"(thal)"
                ]
                
                pdf_name = pdf_file.name.lower()
                is_valid_pdf = False
                
                # Check 1: Specific Form Header (from user provided data)
                if any(m in pdf_content for m in form_markers):
                    is_valid_pdf = True
                
                # Check 2: Content-based validation (Strictly looking for medical markers)
                elif any(m in pdf_content for m in h_clinical):
                    pdf_pred = "High Risk"
                    is_valid_pdf = True
                elif any(m in pdf_content for m in l_clinical):
                    pdf_pred = "Low Risk"
                    is_valid_pdf = True
                elif any(m in pdf_content for m in valid_excel_markers):
                    is_valid_pdf = True
                
                # Check 2: Feature-based validation (If we found the actual numeric data, it's definitely valid)
                if found_count >= 5:
                    is_valid_pdf = True

                # Check 3: Filename-based validation (Fallback for scanned or complex PDFs)
                if not is_valid_pdf:
                    filename_markers = ['heart', 'disease', 'lab', 'report', 'medical', 'clinical', 'data', 'presence', 'diagnostic', 'scan']
                    if any(m in pdf_name for m in filename_markers):
                        is_valid_pdf = True
                        # If filename looks valid but content was unreadable, we still allow it
                    
                if not is_valid_pdf:
                    return JsonResponse({'status': 'error', 'message': 'The uploaded PDF does not appear to contain valid lab or medical data. Please upload a valid report containing heart disease clinical markers.'})

            # 4. FINAL DECISION - Trust Data from PDF First if available
            # Priority 1: Model prediction from PDF numeric data
            # Priority 2: Explicit PDF clinical markers (text-based)
            # Priority 3: Image analysis (primary diagnostic tool)
            
            if extracted_features and pdf_pred:
                # Use the precise model prediction from extracted features
                report.prediction = pdf_pred
                report.risk_factor = pdf_risk
            elif pdf_pred == "High Risk":
                # Fallback to text-based markers if model failed
                report.prediction = "High Risk"
                report.risk_factor = 88.0
            elif pdf_pred == "Low Risk":
                report.prediction = "Low Risk"
                report.risk_factor = 12.0
            elif image_file:
                # Trust the image analysis if no PDF data found
                report.prediction = img_pred_label
                report.risk_factor = img_risk_val
            else:
                report.prediction = "Low Risk"
                report.risk_factor = 12.0

            report.status = "In Progress"

            # 5. Link Doctor
            if doctor_name:
                doc_user = User.objects.filter(username=doctor_name).first()
                if doc_user:
                    report.doctor = doc_user
            
            try:
                report.save()
            except Exception:
                return JsonResponse({'status': 'error', 'message': f'Protocol ID {patient_id} already exists.'}, status=400)
            
            return JsonResponse({
                'status': 'success', 
                'prediction': report.prediction,
                'risk': report.risk_factor,
                'pdf_url': report.pdf_report.url if report.pdf_report else "",
                'message': 'Analysis completed and saved'
            })
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    
    return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)

@login_required
def add_patient(request):
    """
    Creates a new patient record (MedicalReport without image/prediction)
    so they appear in the Pending registry.
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            patient_name = data.get('patient_name')
            patient_id = data.get('patient_id')
            doctor_name = data.get('doctor_name')
            
            # Simple validation
            if not patient_name or not patient_id:
                return JsonResponse({'status': 'error', 'message': 'Missing fields'}, status=400)

            patient_id = patient_id.strip()
            # Block onboarding if:
            # 1. Any record exists for this ID that is not completed (Pending/In Progress)
            # 2. OR a completed record exists from the last 7 days
            one_week_ago = timezone.now() - timedelta(days=7)
            
            existing_active = MedicalReport.objects.filter(patient_id__iexact=patient_id).filter(
                models.Q(prediction__isnull=True) | models.Q(prediction='')
            ).exists()
            
            existing_recent = MedicalReport.objects.filter(
                patient_id__iexact=patient_id, 
                created_at__gte=one_week_ago
            ).exclude(prediction__isnull=True).exclude(prediction='').exists()

            if existing_active:
                return JsonResponse({'status': 'error', 'message': f'Protocol ID {patient_id} is already in the registry.'})
            if existing_recent:
                return JsonResponse({'status': 'error', 'message': 'This patient has already submitted data recently. Please try again after 7 days.'})

            report = MedicalReport(
                patient_name=patient_name,
                patient_id=patient_id,
            )
            
            # Link doctor if found
            if doctor_name:
                # First try direct username match (new behavior)
                doc = User.objects.filter(username=doctor_name, userprofile__role='doctor').first()
                if doc:
                    report.doctor = doc
                else:
                    # Fallback for old behavior or brittle name matching if needed
                    parts = doctor_name.replace("Dr. ", "").split(" ")
                    if len(parts) >= 2:
                        first = parts[0]
                        last = " ".join(parts[1:])
                        doc = User.objects.filter(first_name__iexact=first, last_name__iexact=last, userprofile__role='doctor').first()
                        if doc:
                            report.doctor = doc
            
            try:
                report.save()
            except Exception:
                return JsonResponse({'status': 'error', 'message': f'Protocol ID {patient_id} already exists.'}, status=400)
            
            return JsonResponse({'status': 'success', 'message': 'Patient added'})
            
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
            
    return JsonResponse({'status': 'error', 'message': 'Invalid method'}, status=400)

@login_required
def complete_report(request):
    """
    Manually mark a report as completed.
    This generates a PDF even if no image was uploaded.
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            pid = data.get('patient_id')
            
            report = MedicalReport.objects.filter(patient_id=pid).last()
            if not report:
                return JsonResponse({'status': 'error', 'message': 'Report not found'}, status=404)
            
            if not report.prediction:
                report.prediction = "Low Risk"
            
            report.status = "Completed"
            
            # Generate PDF upon finalization
            display_doctor_name = "Unassigned"
            if report.doctor:
                if report.doctor.first_name or report.doctor.last_name:
                    display_doctor_name = f"Dr. {report.doctor.first_name} {report.doctor.last_name}".strip()
                else:
                    display_doctor_name = report.doctor.username

            # Correct risk factor logic based on prediction
            pred_str = str(report.prediction or "").lower()
            if 'high' in pred_str:
                if not report.risk_factor or report.risk_factor < 50:
                    report.risk_factor = 88.0
                report.prediction = "High Risk"
            else:
                if not report.risk_factor or report.risk_factor > 50:
                    report.risk_factor = 12.0
                report.prediction = "Low Risk"
            
            final_risk_val = report.risk_factor

            pdf_data = {
                'patient_name': report.patient_name,
                'patient_id': report.patient_id,
                'doctor_name': display_doctor_name,
                'prediction': report.prediction,
                'risk_factor': final_risk_val,
                'date': datetime.now().strftime("%Y-%m-%d %H:%M"),
                'image_path': report.image.path if report.image else None
            }
            pdf_buffer = generate_pdf_report(pdf_data)
            filename = f"Final_Report_{pid}_{report.id}.pdf"
            
            # This ensures we overwrite any temporary/uploaded PDF with the final official report
            report.pdf_report.save(filename, ContentFile(pdf_buffer.getvalue()))
            
            report.save()
            return JsonResponse({'status': 'success', 'message': 'Report finalized', 'pdf_url': report.pdf_report.url})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    return JsonResponse({'status': 'error', 'message': 'Invalid method'}, status=400)

