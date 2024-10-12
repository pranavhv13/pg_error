import base64, qrcode
import json
import hashlib
import datetime
import uuid
import logging
from http import client
from PIL import Image, ImageDraw, ImageFont
from django.apps import apps
from django.shortcuts import get_object_or_404, render, redirect
from django.http import HttpResponse
import razorpay
import requests
from valid_entry import settings
#from my_app.razorpay_integration import initiate_payment
from .models import Event, FormConfig
from .forms import create_dynamic_form
from django.views.decorators.csrf import csrf_exempt
from django.urls import reverse


def index(request):
    return render(request,'index.html')

def contact(request):
    return render(request,'contact.html')

def events(request):
    upcoming_events = Event.objects.filter(isDone=False).order_by('date')
    completed_events = Event.objects.filter(isDone=True).order_by('-date')
    return render(request, 'events.html', {
        'upcoming_events': upcoming_events,
        'completed_events': completed_events,
    })

def event_info(request, event_id):
    event = Event.objects.get(id=event_id) 
    return render(request, 'event_info.html', {'event': event})

def sucess(request):
    return render(request, 'sucess.html')


def generate_tran_id():
    """To genarate a unique order number"""
    uuid_part = str(uuid.uuid4()).split('-')[0].upper()  # Get a part of the UUID
    now = datetime.datetime.now().strftime('%Y%m%d')
    return f"TRX{now}{uuid_part}"

def generate_checksum(data, salt_key, salt_index):
    """To Genarate checksum"""
    checksum_str = data + '/pg/v1/pay' + salt_key
    checksum = hashlib.sha256(checksum_str.encode()).hexdigest() + '###' + salt_index
    return checksum

logger = logging.getLogger('payment')

from django.shortcuts import render, get_object_or_404, redirect
from .models import Event, FormConfig, Ticket
from .forms import create_dynamic_form

@csrf_exempt
def register_event(request, event_id, form_id):
    # Fetch the event and form configuration
    event = get_object_or_404(Event, id=event_id)
    form_config = get_object_or_404(FormConfig, id=form_id)
    FORM_CONFIGS = form_config.fields  

    # Create the dynamic form
    DynamicForm = create_dynamic_form(form_id)

    if request.method == 'POST':
        form = DynamicForm(request.POST, request.FILES) 
        if form.is_valid():

            submitted_data = form.cleaned_data

            # for uploaded img , file or date
            uploaded_file = None
            file_field_name = None
            uploaded_date = None
            date_field_name = None

            # Iterate through fields in the form configuration
            for field in FORM_CONFIGS['fields']:
                if field['type'] == 'image' or field['type'] == 'file':  # Check for file/image type
                    file_field_name = field['name']  # Get the name of the file field
                    uploaded_file = request.FILES.get(file_field_name)  # Retrieve the uploaded file
            
            for field in FORM_CONFIGS['fields']:
                if field['type'] == 'date':  
                    date_field_name = field['name'] 
                    uploaded_date = submitted_data.get(date_field_name)  
            
            # Creating a unique ticket ID
            ticket_id = f"evt_{event_id}_tk_{Ticket.objects.count() + 1}"
            
            #encryption of ticket_id
            byte_string = ticket_id.encode('utf-8')
            base64_bytes = base64.b64encode(byte_string)
            enc_tk_id = base64_bytes.decode('utf-8')

            # Save ticket details to the Ticket model
            Ticket.objects.create(
                ticket_id=ticket_id,
                enc_tk_id = f"www.valid-entry/tk/{enc_tk_id}",
                event_id=event,
                ticket_data={k: v for k, v in submitted_data.items() if k != file_field_name and k != date_field_name},  # Exclude the file data from JSON
                uploaded_file=uploaded_file,
                date_field=uploaded_date  
            )
    

            # Prepare payment initiation
            
            amount = 1000  # Example amount in rupees (replace with your actual amount)
            # callback_url = request.build_absolute_uri(reverse('payment:sucess'))
            callback_url = 'http://127.0.0.1:8000/sucess'
            
            payload = {
                "merchantId": settings.PHONEPE_MERCHANT_ID,
                "merchantTransactionId": generate_tran_id(),
                "merchantUserId": "USR1231",
                "amount": amount * 100,  # In paisa
                "redirectUrl": callback_url,
                "redirectMode": "POST",
                "callbackUrl": callback_url,
                "mobileNumber": "9800278886",
                "paymentInstrument": {
                    "type": "PAY_PAGE"
                }
            }
            
            data = base64.b64encode(json.dumps(payload).encode()).decode()
            checksum = generate_checksum(data, settings.PHONEPE_MERCHANT_KEY, settings.SALT_INDEX)
            final_payload = {
                "request": data,
            }
            
            headers = {
                'Content-Type': 'application/json',
                'X-VERIFY': checksum,
            }
            
            try:
                response = requests.post(settings.PHONEPE_INITIATE_PAYMENT_URL + '/pg/v1/pay', headers=headers, json=final_payload)
                data = response.json()

                if data['success']:
                    url = data['data']['instrumentResponse']['redirectInfo']['url']
                    return redirect(url)
                else:
                    logger.error(f"Payment initiation failed: {data}")
                    return redirect('/failed')

            except Exception as e:
                logger.error(f"Error initiating payment: {e}")
                return redirect('/')

    else:
        form = DynamicForm()

    return render(request, 'register_event.html', 
                  {'event': event, 
                   'form': form, 
                   'title': FORM_CONFIGS['title'], 
                   'form_id': form_id,
                   'event_form_id': event.form_id })


@csrf_exempt
def payment_callback(request):
    if request.method != 'POST':
        logger.error("Invalid request method: %s", request.method)
        return redirect('/')

    try:
        data = request.POST.dict()  # Convert QueryDict to a regular dictionary
        logger.info(data)
        
        # Assuming payment success based on response code
        if data.get('checksum') and data.get('code') == "PAYMENT_SUCCESS":
            # Fetch the ticket by transaction ID if needed
            transaction_id = data.get('merchantTransactionId')
            ticket = Ticket.objects.filter(ticket_id__icontains=transaction_id).first()

            if ticket:
                # Mark the ticket as paid or do other necessary updates
                ticket.is_paid = True
                ticket.save()

                # Retrieve the submitted data for displaying in success page
                submitted_data = ticket.ticket_data
                ticket_id = ticket.ticket_id
                
                return render(request, 'success.html', {
                    'submitted_data': submitted_data,
                    'ticket_id': ticket_id,
                })
        
        # If payment failed, redirect to the failure page or handle error
        logger.info("Payment failed: %s", data)
        return render(request, 'failed.html')

    except Exception as e:
        logger.error(f"Error in payment callback: {e}")
        return render(request, 'failed.html')





