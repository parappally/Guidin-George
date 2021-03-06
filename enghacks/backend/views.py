from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import viewsets
from rest_framework.views import APIView
from django.contrib.auth import get_user_model
from backend.serializers import UserLoginSerializer
from rest_framework.response import Response
from django.conf import settings
from backend.models import DirectionThread, Place
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from datetime import datetime, timedelta
from django.utils import timezone
import re
import googlemaps
from geopy.distance import geodesic


class UserLoginViewSet(viewsets.ModelViewSet):
    User = get_user_model()
    queryset = User.objects.all()
    serializer_class = UserLoginSerializer

class SMSDirectionsViewSet(viewsets.ModelViewSet):
    User = get_user_model()
    serializer_class = UserLoginSerializer

    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    default_number = settings.TWILIO_DEFAULT_CALLERID
    client = Client(account_sid, auth_token)

    max_time_threshold = timedelta(hours=2)
    gmaps = googlemaps.Client(key=settings.GOOGLE_MAPS_KEY)

    def list(self, request):
        from_num = request.query_params.get('From', None)
        if from_num:
            reply_number = self.format_phone(from_num)
        else:
            raise('Not SMS')
        request_body = request.query_params.get('Body')
        return_body = ''
        request_user = self.User.objects.get(phone = reply_number)
        user_threads = DirectionThread.objects.filter(
            user = request_user
        ).order_by('-date_time')
        latest_thread = user_threads[0] if len(user_threads) else None
        if request_body == 'Reset' or not latest_thread or latest_thread.current_step == 'ARRIVED':
            latest_thread = self._create_new_thread(request_user)
            return_body = 'Hi {}, I\'m Guidin\' George! What\'s your location?'.format(
                request_user.first_name
            )
            self.send_text(return_body, reply_number)
            return Response(request.data)

        if latest_thread:
            last_thread_age = timezone.now() - latest_thread.date_time
            if last_thread_age > self.max_time_threshold:
                return_body = 'Your last session was {0} ago, starting new session.'.format(last_thread_age) +\
                    'Hi {}, I\'m Guidin\' George! What\'s your location?'.format(
                    request_user.first_name
                )
                latest_thread = self._create_new_thread(request_user)
                self.send_text(return_body, reply_number)
                return Response(request.data)

        if latest_thread.current_step == 'USER_LOCATION':
            latest_thread.start_location = request_body
            latest_thread.save
            return_body = 'Where would you like to go? (Please enter an address)'
            self.send_text(return_body, reply_number)
            latest_thread.increment_step()
            return Response(request.data)

        elif latest_thread.current_step == 'DESTINATION':
            user_location = latest_thread.start_location
            user_lat, user_lng = self.geocode_address(user_location)
            self.send_text('Guidin\' George is thinking...', reply_number)
            places_list = self.get_places_lst(
                query=request_body,
                user_lat=user_lat,
                user_lng=user_lng,
                radius=5,
                direction_thread=latest_thread
            )
            return_body = self.places_list_to_string(places_list)
            if len(places_list) == 0:
                invalid_body = 'I did not find any results for \"{}\". Please enter another destination.'.format(
                        request_body
                    )
                self.send_text(invalid_body, reply_number)
            else:
                self.send_text(return_body, reply_number)
                latest_thread.increment_step()
            return Response(request.data)

        elif latest_thread.current_step == 'DEST_CHOICES':
            places_list = latest_thread.places_list.order_by('distance')
            if self.is_integer(request_body):
                choice_number = int(request_body) - 1
                if choice_number < len(places_list):
                    selected_dest = places_list[choice_number]
                    latest_thread.end_location = selected_dest.address
                    pending_body = 'You have selected {}. Guidin\' George is thinking...'.format(
                        latest_thread.end_location
                    )
                    self.send_text(pending_body, reply_number)
                    return_body = self.lst_of_directions(
                        latest_thread.start_location,
                        latest_thread.end_location,
                    )
                    self.send_text(return_body, reply_number)
                    latest_thread.increment_step()
                    return Response(request.data)
                    
            return_body = 'Invalid choice, please enter a number between 1 and {}'.format(
                len(places_list)
            )
            self.send_text(return_body, reply_number)
            return Response(request.data)        

        elif latest_thread.current_step == 'IN_TRANSIT':
            return_body = 'You have arrived at {}. Thank you for using Guidin\' George!'.format(
                latest_thread.end_location
            )
            self.send_text(return_body, reply_number)
            latest_thread.increment_step()
        return Response(request.data)


    def is_integer(self, text):
        try: 
            int(text)
            return True
        except ValueError:
            return False


    def _create_new_thread(self, request_user):
        new_thread = DirectionThread.objects.create(
                    user = request_user
        )
        return new_thread

    def send_text(self, return_body, reply_number):
        self.client.messages \
            .create(
                    body=return_body,
                    from_=self.default_number,
                    to=reply_number
            )

    def format_phone(self, phone_number):
        # strip non-numeric characters
        phone = re.sub(r'\D', '', phone_number)
        # remove leading 1 (area codes never start with 1)
        phone = phone.lstrip('1')
        return '{}{}{}'.format(phone[0:3], phone[3:6], phone[6:])
   
    def lst_of_directions(self, origin, destination):
        directionsObj = self.gmaps.directions(origin, destination, "walking")
        # return(directionsObj[0]['overview_polyline']['warnings'])
        x = (directionsObj[0]['legs'][0]['steps'])

        distance_lst = []
        for elem in x:
            distance_lst.append(str(elem['distance']['text']))

        step_lst_html = []
        for elem in x:
            step_lst_html.append(str(elem['html_instructions']))

        step_lst = []
        for elem in step_lst_html:
            elem = re.sub('<.*?>', ' ', elem)
            step_lst.append(elem)

        combined_lst = []
        for index in range(len(step_lst)):
            distanceStep = step_lst[index] + "(" + distance_lst[index] + ")"
            combined_lst.append(distanceStep)

        full_string = "\n".join(combined_lst)
        intro = "Here are the directions: \n"
        outro = 'Text \"Complete\" when you have arrived!'
        return(intro + full_string + '\n' + outro)
      
    def geocode_address(self, address):
        geocode = self.gmaps.geocode(address)
        lat = geocode[0]['geometry']['location']['lat']
        lng = geocode[0]['geometry']['location']['lng']
        return lat, lng

    def calculate_distance(self, lat1, long1, lat2, long2):
        place1 = (lat1, long1)
        place2 = (lat2, long2)
        distance_in_km = round(geodesic(place1, place2).km, 2)
        return distance_in_km
        # if distance_in_km < 1:
        #     return str(distance_in_km * 1000) + "m"
        # else:
        #     return str(distance_in_km) + " km"

    def get_places_lst(self, query, user_lat, user_lng, radius, direction_thread):
        location = str(user_lat) + ', ' + str(user_lng)
        places_result = self.gmaps.places(query, location, radius)
        places_array = []
        for place in places_result['results']:
            address = place['formatted_address']
            name = place['name']
            lat = float(place['geometry']['location']['lat'])
            lng = float(place['geometry']['location']['lng'])
            distance = self.calculate_distance(
                lat1=user_lat,
                long1=user_lng,
                lat2=lat,
                long2=lng,
            )
            new_place = Place.objects.create(
                address=address,
                name=name,
                direction_thread=direction_thread,
                distance=distance,
            )
            places_array.append(new_place)
        places_array.sort(key=lambda place: place.distance)
        return places_array[:5]

    def places_list_to_string(self, list_of_places):
        counter = 1
        text_lst = []
        for place in list_of_places:
            place_string = ""
            place_string += '[' + str(counter) + '] ' + place.name + "," + place.address + "," + '(' + str(place.distance) + ' km' + ')'
            text_lst.append(place_string)
            counter += 1
        full_string = "\n".join(text_lst)
        full_string = 'Please select a number from the list (e.g: 1) \n' + full_string
        return full_string

