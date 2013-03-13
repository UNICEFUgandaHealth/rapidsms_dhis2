import settings
import urllib2, base64
from django.template import Context
from xml.dom.minidom import parseString
from django.template.loader import get_template as load_xml_template
from rapidsms_xforms.models import XFormSubmissionValue ,XFormSubmission
from mtrack.models import XFormSubmissionExtras
from datetime import timedelta,datetime
from dhis2.models import Dhis2_Mtrac_Indicators_Mapping ,Dhis2_Reports_Report_Task_Log ,Dhis2_Reports_Submissions_Log
from xml.parsers.expat import ExpatError

HMIS033B_REPORT_XML_TEMPLATE      = "h033b_reporter.xml"
DATA_VALUE_SETS_URL               = u'/api/dataValueSets'
DEFAULT_ORG_UNIT_ID_SCHEME        = u'uuid'
ISO_8601_UTC_FORMAT               = u'%s-%s-%sT%s:%s:%sZ'
HMIS_033B_PERIOD_ID               = u'%dW%d'
ERROR_MESSAGE_NO_HMS_INDICATOR    = u'No valid HMS033b indicators reported for the submission'
ERROR_MESSAGE_ALL_VALUES_IGNORED  = u'All values rejected by remote server'
ERROR_MESSAGE_SOME_VALUES_IGNORED = u'Some values rejected by remote server'
ERROR_MESSAGE_CONNECTION_FAILED   = u'Error communicating with the remote server'
ERROR_MESSAGE_UNEXPECTED_ERROR    = u'Unexpected error while submitting reports to DHIS2'
ERROR_MESSAGE_UNEXPECTED_RESPONSE_FROM_DHIS2   = u'Unexpected response from DHIS2'



class H033B_Reporter(object):
  
  def __init__(self): 
    self.url       = settings.DHIS2_BASE_URL + DATA_VALUE_SETS_URL
    self.user_name = settings.DHIS2_REPORTER_USERNAME
    self.password  = settings.DHIS2_REPORTER_PASSWORD
    self.headers = {
        'Content-type': 'application/xml',
        'Authorization': 'Basic ' + base64.b64encode("%s:%s"%(self.user_name,self.password))
    }
  
  def send(self, data):
    request = urllib2.Request(self.url, data = data, headers = self.headers)
    # Make POST call instead of get
    request.get_method = lambda: "POST"
    return urllib2.urlopen(request)

  def submit_report(self, data):
    xml_request = self.generate_xml_report(data)
    response = self.send(xml_request)
    return self.parse_submission_response(response.read(),xml_request)
  
  def parse_submission_response(self,response_xml,request_xml):   

    try: 
      dom      = parseString(response_xml)
      result   = dom.getElementsByTagName('dataValueCount')[0]
      imported = int(result.getAttribute('imported'))
      updated  = int(result.getAttribute('updated'))
      ignored  = int(result.getAttribute('ignored'))
      error    = None

      conflicts = dom.getElementsByTagName('conflict')
      if conflicts : 
        error   = ''
        for conflict in conflicts : 
          error+='%s  : %s\n'%(conflict.getAttribute('object') ,conflict.getAttribute('value'))

      result                = {}
      result['imported']    = imported
      result['updated']     = updated
      result['ignored']     = ignored
      result['error']       = error
      result['request_xml'] = request_xml
    except ExpatError ,e :
      e.request_xml = request_xml
      raise e

    return result
    
  def generate_xml_report(self,data):
    template = load_xml_template(HMIS033B_REPORT_XML_TEMPLATE)
    data = template.render(Context(data))
    return data
    
  def get_utc_time_iso8601(self,time_arg):
    year_str   = str(time_arg.year)
    month_str  = str(time_arg.month)
    day_str    = str(time_arg.day)
    hour_str   = str(time_arg.hour)
    minute_str = str(time_arg.minute)
    second_str = str(time_arg.second)
    
    if len(month_str) <2 : 
      month_str = str(0)+ month_str
    if len(day_str) <2 : 
      day_str = str(0)+ day_str
    if len(hour_str) <2 : 
      hour_str = str(0)+ hour_str
    if len(minute_str) <2 : 
      minute_str = str(0)+ minute_str
    if len(second_str) <2 : 
      second_str = str(0)+ second_str
      
    return ISO_8601_UTC_FORMAT%(year_str,month_str,day_str,hour_str,minute_str,second_str)
  
  @classmethod  
  def get_week_period_id_for_sunday(self, date):
    year    = date.year
    weekday = int(date.strftime("%W")) + 1
    return HMIS_033B_PERIOD_ID%(year,weekday)
    
  @classmethod
  def get_period_id_for_submission(self,date):
    return self.get_week_period_id_for_sunday(self.get_last_sunday(date))   
    
  @classmethod
  def get_last_sunday(self, date):
    offset_from_last_sunday = date.weekday()+1 
    last_sunday = date  - timedelta(days= offset_from_last_sunday)
    return last_sunday
  
  def get_reports_data_for_submission(self,submission,orgUnitIdScheme=DEFAULT_ORG_UNIT_ID_SCHEME):
    data = {}
    data['orgUnit']           = submission.facility.uuid
    data['completeDate']      = self.get_utc_time_iso8601(submission.created)
    data['period']            = self.get_period_id_for_submission(submission.created)
    data['orgUnitIdScheme']   = orgUnitIdScheme
    
    self.set_data_values_from_submission_value(data,submission)
    
    if not data['dataValues'] : 
      raise LookupError(ERROR_MESSAGE_NO_HMS_INDICATOR)
    
    return data
    
  def set_data_values_from_submission_value(self,data,submission):
    submission_values  = XFormSubmissionValue.objects.filter(submission=submission)
    data['dataValues']    = []
        
    for submission_value in submission_values : 
      dataValue = self.get_attibute_values_for_submission(submission_value)
      if dataValue :
        data['dataValues'].append(dataValue)
        
  def get_attibute_values_for_submission(self, submission_value):
    data_value      = {}
    attribute = submission_value.attribute
    dhis2_mapping   = Dhis2_Mtrac_Indicators_Mapping.objects.filter(eav_attribute=attribute)
    
    if dhis2_mapping:
      element_id                        = dhis2_mapping[0].dhis2_uuid
      combo_id                          = dhis2_mapping[0].dhis2_combo_id
      data_value['dataElement']         = element_id
      data_value['value']               = submission_value.value
      data_value['categoryOptionCombo'] = combo_id
      
    return data_value
    
  def get_submissions_in_date_range(self,from_date,to_date):
    submissions = XFormSubmission.objects.filter(created__range=[from_date, to_date] )  
    xtras = XFormSubmissionExtras.objects.filter(submission__in=submissions).exclude(facility=None)
    valid_submission_ids = list(set(xtras.values_list('submission', flat=True)))
    
    reported_submissions = Dhis2_Reports_Submissions_Log.objects.filter(
      submission_id__in=valid_submission_ids,
      result=Dhis2_Reports_Submissions_Log.SUCCESS)
      
    reported_submissions_ids = list(set(reported_submissions.values_list('submission_id',flat=True)))
    
    for submission_id in reported_submissions_ids : 
      valid_submission_ids.remove(submission_id)
      
    filtered_Submissions = submissions.filter(id__in=valid_submission_ids)
    
    return self.__preprocess_submissions(filtered_Submissions)

  def __set_submissions_facility(self,submissions):
    for submission in submissions : 
      subextra = XFormSubmissionExtras.objects.get(submission=submission)
      submission.facility = subextra.facility
      
  def __preprocess_submissions(self,submissions):
    # Sort by xform,created,facility id
    submissions = submissions.order_by('xform','created') 
    submissions_list = list(submissions)
    self.__set_submissions_facility(submissions_list)
    
    sorter_by_facility = lambda submission : submission.facility.id
    submissions_list  =sorted(submissions_list,key=sorter_by_facility)
    
    cleaned_list = []
    count =0
    submissions_count = len(submissions_list)
    
    for count in range(submissions_count): 
      if count == submissions_count-1 : 
        cleaned_list.append(submissions_list[count])
      elif not self.__are_submissions_duplicate(submissions_list[count] ,submissions_list[count+1] ):
        cleaned_list.append(submissions_list[count]) 
        
    return cleaned_list
  
  
  def __are_submissions_duplicate(self,submission1,submission2):
    return submission1.xform.id == submission2.xform.id and submission1.facility.id == submission2.facility.id
    

  def log_submission_started(self) : 
    self.current_task =  Dhis2_Reports_Report_Task_Log.objects.create()

  def log_submission_finished(self,submission_count, status, description='') :
    log_record                       = self.current_task
    log_record.time_finished         = datetime.now()
    log_record.number_of_submissions = submission_count
    log_record.status                = status
    log_record.description           = description
    log_record.save()

  def submit_report_and_log_result(self,submission):
    success = False
    
    try : 
      data =self.get_reports_data_for_submission(submission)
      result = self.submit_report(data)
      accepted_attributes_values = int(result['updated']) + int(result['imported'])
      log_message=''

      if result['error'] :      
        log_result  = Dhis2_Reports_Submissions_Log.ERROR
        log_message = result['error']
      elif not accepted_attributes_values : 
        log_message = ERROR_MESSAGE_ALL_VALUES_IGNORED
        log_result  = Dhis2_Reports_Submissions_Log.ERROR
      elif result['ignored'] : 
        log_message = ERROR_MESSAGE_SOME_VALUES_IGNORED
        log_result  = Dhis2_Reports_Submissions_Log.SOME_ATTRIBUTES_IGNORED
      else :
        log_result  = Dhis2_Reports_Submissions_Log.SUCCESS
        success =True
      
      requestXML = result['request_xml']      
    except ExpatError,e : 
      error_message = type(e).__name__ +":"+ str(e)
      log_message = "%s\n%s"%(ERROR_MESSAGE_UNEXPECTED_RESPONSE_FROM_DHIS2,error_message)
      log_result = Dhis2_Reports_Submissions_Log.ERROR
      requestXML = e.request_xml 
    except LookupError ,e :
      error_message = type(e).__name__ +":"+ str(e)
      log_message = error_message
      log_result = Dhis2_Reports_Submissions_Log.INVALID_SUBMISSION_DATA
      requestXML=None
    
    # Do not log the request XML if success
    Dhis2_Reports_Submissions_Log.objects.create(
      task_id = self.current_task,
      submission_id = submission.id,
      reported_xml = requestXML if not success else None, 
      result = log_result,
      description =log_message
    )
        
    return log_result == Dhis2_Reports_Report_Task_Log.SUCCESS
    
  
  def initiate_weekly_submissions(self,date=datetime.now()):
    last_monday = self.get_last_sunday(date) + timedelta(days=1)
    submissions_for_last_week = self.get_submissions_in_date_range(last_monday, date)

    self.log_submission_started()
    successful_submissions  =  0
    connection_failed = False
    status = Dhis2_Reports_Report_Task_Log.SUCCESS
    description = ''
    try : 
      for submission in submissions_for_last_week:
        try :
          if self.submit_report_and_log_result(submission) :            
            successful_submissions +=1          
        except urllib2.URLError , e: 
          exception = type(e).__name__ +":"+ str(e)
          connection_failed = True
          status = Dhis2_Reports_Report_Task_Log.FAILED
          description = ERROR_MESSAGE_CONNECTION_FAILED + ' Exception : '+exception
          break
        except Exception ,e : 
          exception = type(e).__name__ +":"+ str(e)
          connection_failed = True
          status = Dhis2_Reports_Report_Task_Log.FAILED
          description = ERROR_MESSAGE_UNEXPECTED_ERROR + ' Exception : '+exception
          raise e
    finally : 
      self.log_submission_finished(
        submission_count=successful_submissions,
        status= status,
        description=description)