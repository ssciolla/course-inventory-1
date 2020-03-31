# standard libraries
import copy, json, logging
from typing import Dict, Sequence, Tuple
from json.decoder import JSONDecodeError

# third-party libraries
import pandas as pd
from requests_futures.sessions import FuturesSession
from concurrent.futures import as_completed, Future

logger = logging.getLogger(__name__)


def unnest_enrollment(enroll_dict: Dict) -> Tuple[Dict, ...]:
    flat_enroll_dict = {
        'canvas_id': int(enroll_dict['_id']),
        'user_id': int(enroll_dict['user']['_id']),
        'course_id': int(enroll_dict['course']['_id']),
        'course_section_id': int(enroll_dict['section']['_id']),
        'role_type': enroll_dict['type'],
        'workflow_state': enroll_dict['state']
    }

    user_data = enroll_dict['user']
    flat_user_dict = {
        'canvas_id': int(user_data['_id']),
        'name': user_data['name']
    }

    section_data = enroll_dict['section']
    flat_section_dict = {
        'canvas_id': int(section_data['_id']),
        'name': section_data['name'],
    }
    return (flat_enroll_dict, flat_user_dict, flat_section_dict)


class AsyncEnrollGatherer:

    def __init__(
        self,
        course_ids: Sequence[int],
        access_token: str,
        complete_url: str,
        gql_query: str,
        enroll_page_size: int = 75,
        num_workers: int = 8
    ):
        self.course_ids = sorted(course_ids)
        self.complete_url = complete_url
        self.gql_query = gql_query
        self.num_workers = num_workers
        self.default_params = {
            'access_token': access_token,
            'query': gql_query,
            'variables': {
                'courseID': None,
                'enrollmentPageSize': enroll_page_size,
                'enrollmentPageCursor': ''
            }
        }
        self.course_enrollments: Sequence[Dict[Union[Dict, int]]] = {}

    def get_complete_course_ids(self) -> Sequence[int]:
        complete_course_ids = []
        for course_id in self.course_enrollments.keys():
            enroll_meta_dict = self.course_enrollments[course_id]
            if not enroll_meta_dict['page_info']['hasNextPage']:
                complete_course_ids.append(course_id)
        return complete_course_ids

    def get_uncompleted_course_ids(self) -> Sequence[int]:
        course_ids = []

        # Get unstarted course_ids
        for course_id in self.course_ids:
            if course_id not in self.course_enrollments.keys():
                course_ids.append(course_id)

        # Get in-progress course ids
        for course_id in self.course_enrollments.keys():
            course_enrollment_dict = self.course_enrollments[course_id]
            if course_enrollment_dict['page_info']['hasNextPage']:
                course_ids.append(course_id)

        return course_ids

    def parse_enrollment_response(self, future_response: Future) -> None:
        # Check for irregular results
        response = future_response.result()
        status_code = response.status_code
        problem_encountered = False
        if status_code != 200:
            logger.warning(f'Received irregular status code: {status_code}')
            logger.debug(response.text)
            problem_encountered = True
        else:
            try:
                response_data = json.loads(response.text)
            except JSONDecodeError:
                logger.warning('JSONDecodeError encountered')
                problem_encountered = True

        if problem_encountered:
            logger.warning('No data will be stored, and the request will be re-tried')
        else:
            response_course_id = int(response_data['data']['course']['_id'])

            enrollments_connection = response_data['data']['course']['enrollmentsConnection']
            enrollment_dicts = enrollments_connection['nodes']
            enrollment_page_info = enrollments_connection['pageInfo']

            if response_course_id not in self.course_enrollments.keys():
                # Create new in-progress record
                self.course_enrollments[response_course_id] = {
                    'enrollments': enrollment_dicts,
                    'page_info': enrollment_page_info,
                    'num_pages': 1
                }
            else:
                # Update existing in-progress record
                self.course_enrollments[response_course_id]['enrollments'] += enrollment_dicts
                self.course_enrollments[response_course_id]['page_info'] = enrollment_page_info
                self.course_enrollments[response_course_id]['num_pages'] += 1

    def make_requests(self, course_ids: Sequence[int]) -> None:
        with FuturesSession(max_workers=self.num_workers) as session:
            responses = []
            for course_id in course_ids:
                # Prep params
                params = copy.deepcopy(self.default_params)
                params['variables']['courseID'] = course_id
                if course_id in self.course_enrollments.keys():
                    course_enrollment_dict = self.course_enrollments[course_id]
                    enroll_page_info = course_enrollment_dict['page_info']
                    params['variables']['enrollmentPageCursor'] = enroll_page_info['endCursor']

                logger.debug(params['variables'])
                response = session.post(self.complete_url, json=params)
                responses.append(response)

            for completed_response in as_completed(responses):
                self.parse_enrollment_response(completed_response)

                # Log process status
                logger.info(f'# started courses: {len(self.course_enrollments)}')
                logger.info(f'# completed courses: {len(self.get_complete_course_ids())}')

    def generate_output(self) -> Tuple[pd.DataFrame, ...]:
        logger.debug('generate_output')
        enrollment_records = []
        user_records = []
        section_records = []

        for course_id in self.course_enrollments.keys():
            enrollment_dicts = self.course_enrollments[course_id]['enrollments']
            for enrollment_dict in enrollment_dicts:
                enrollment_record, user_record, section_record = unnest_enrollment(enrollment_dict)
                enrollment_records.append(enrollment_record)
                user_records.append(user_record)
                section_records.append(section_record)

        enrollment_df = pd.DataFrame(enrollment_records)
        user_df = pd.DataFrame(user_records).drop_duplicates(subset=['canvas_id'])
        section_df = pd.DataFrame(section_records).drop_duplicates(subset=['canvas_id'])
        return (enrollment_df, user_df, section_df)

    def gather(self) -> None:
        logger.info('** AsyncEnrollGatherer')
        logger.info('Gathering enrollment data for courses asynchronously with GraphQL')

        prev_course_id_lists = []
        more_to_gather = True

        while more_to_gather:
            course_ids_to_process = sorted(self.get_uncompleted_course_ids())

            if len(course_ids_to_process) == 0:
                more_to_gather = False
                logger.info('Enrollment records for the course IDs have been gathered')
            else:
                if (len(prev_course_id_lists) > 2) and (prev_course_id_lists[0] == course_ids_to_process) and (prev_course_id_lists[1] == course_ids_to_process):
                    logger.warning('A few course IDs could not be processed')
                    logger.warning(course_ids_to_process)
                    more_to_gather = False
                else:
                    self.make_requests(course_ids_to_process)
            prev_course_id_lists = [course_ids_to_process] + prev_course_id_lists
