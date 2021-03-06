import csv
import json
import logging
import os
import redis
import traceback
import urllib
import xml.etree.ElementTree as ET
from kgx import NeoTransformer, JsonTransformer
from neo4jrestclient.client import GraphDatabase
import requests
from requests_cache import CachedSession
from typing import List, Dict
import hashlib

logger = logging.getLogger (__name__)

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

Config = Dict


class Debreviator:
    """ Expand certain abbreviations to increase our hit rate."""
    def __init__(self):
        self.decoder = {
            "bmi" : "body mass index"
        }

    def decode(self, text):
        for key, value in self.decoder.items():
            text = text.replace(key, value)
        return text


class TOPMedStudyAnnotator:
    """
    Annotate TOPMed study data with semantic knowledge graph linkages.

    """
    def __init__(self, config: Config):
        self.normalizer = config['normalizer']
        self.annotator = config['annotator']
        self.synonym_service = config['synonym_service']
        self.db_url = config['db_url']
        self.username = config['username']
        self.password = config['password']
        self.redis_host = config['redis_host']
        self.redis_port = config['redis_port']
        self.redis_password = config['redis_password']
        self.debreviator = Debreviator ()

    def load_data_dictionary (self, input_file : str) -> Dict:
        """
        This loads a data dictionary. It's unclear if this will be useful going forwar. But for now,
        it demonstrates how much of the rest of the  pipeline might be applied to data dictionaries.
        """
        tree = ET.parse(input_file)
        root = tree.getroot()
        study_id = root.attrib['study_id']
        return [{
            "study_id"    : study_id,
            "variable_id" : variable.attrib['id'],
            "variable"    : variable.find ('name').text,
            "description" : variable.find ('description').text.lower (),
            "identifiers" : {}
        } for variable in root.iter('variable') ]

    def load_csv (self, input_file : str) -> Dict:
        """
        Load a CSV. We had a CSV representation of some harmonized variables. This  is likely
        superseded by the approach below. Pending confirmation to delete this code.
        """
        response = []
        with open(input_file, newline='') as csvfile:
            reader = csv.DictReader(csvfile, delimiter='\t',)
            for row in reader:
                logger.debug (row)
                """ VARNAME	VARDESC	TYPE	UNITS	VARIABLE_SOURCE	SOURCE_VARIABLE_ID	VARIABLE_MAPPING	VALUES """
                variable_source = row['VARIABLE_SOURCE']
                source_variable_id = row['SOURCE_VARIABLE_ID']
                response.append ({
                    "study_id"      : 'TODO-???',
                    "variable_id"   : row['VARNAME'],
                    "variable_name" : row['VARNAME'],
                    "description"   : row['VARDESC'],
                    "type"          : row['TYPE'],
                    "units"         : row['UNITS'],
                    "xref"          : f"{variable_source}:{source_variable_id}",
                    "identifiers"   : {}
                })
        return response

    def load_tagged_variables (self, input_file : str) -> Dict:
        """
        Load tagged variables.
          Presumes a harmonized variable list as a CSV file as input.
          A naming convention such that <prefix>_variables_<version>.csv will be the form of the filename.
          An adjacent file called <prefix>_tags_<version.json will exist.

          :param input_file: A list of study variables linked to tags, studies, and other relevant data.
          :returns: Returns variables, a list of parsed variable dictionaries and tags, a list of parsed
                    tag definitions linked to the variabels.
        """
        tags_input_file = input_file.replace (".csv", ".json").replace ("_variables_", "_tags_")
        if not os.path.exists (tags_input_file):
            raise ValueError (f"Accompanying tags file: {tags_input_file} must exist.")
        variables = []
        headers = "tag_pk 	tag_title 	variable_phv 	variable_full_accession 	dataset_full_accession 	study_full_accession 	study_name 	study_phs 	study_version 	created 	modified"
        with open(input_file, newline='') as csvfile:
            reader = csv.DictReader(csvfile, delimiter='\t',)
            for row in reader:
                better_row = { k.strip () : v for  k, v in row.items () }
                row = better_row
                logger.debug (f"{json.dumps(row, indent=2)}")
                variables.append ({
                    "study_id"               : f"TOPMED.STUDY:{row['study_full_accession']}",
                    "tag_pk"                 : row['tag_pk'],
                    "study_name"             : row['study_name'],
                    "study_version"          : row['study_version'],
                    "dataset_full_accession" : row['dataset_full_accession'],
                    "variable_id"            : f"TOPMED.VAR:{row['variable_full_accession']}",
                    "variable_phv"           : row['variable_phv'],
                    "identifiers"            : {}
                })

        tags = []
        with open(tags_input_file, "r") as stream:
            tags = json.load (stream)
        for tag in tags:
            for f, v in tag['fields'].items ():
                tag[f] = v
            del tag[f]
            tag['id'] = f"TOPMED.TAG:{tag['pk']}"
            tag['identifiers'] = {}
        return variables, tags

    def normalize (self, http_session, curie, url, variable) -> None:
        """ Given an identifier (curie), use the Translator SRI node normalization service to
            find a preferred identifier, equivalent identifiers, and biolink model types for the node.

            :param http_session: A requests session to use for HTTP requests.
            :param curie: The identifier to normalize.
            :param url: The URL to use.
            :param variable: The variable in which to record the normalized identifier.
        """
        """
        Added blank_preferred_id to keep terms in the KG that fail to normalize.
        """
        blank_preferred_id = {
            "label": "",
            "equivalent_identifers": [],
            "type": ['named_thing'] # Default NeoTransformer category
        }
        """ Normalize the identifier with respect to the BioLink Model. """
        try:
            logger.debug(f"Normalizing: {curie}")
            normalized = http_session.get(url).json ()
            """ Record normalized results. """
            normalization = normalized.get(curie, {})
            preferred_id = normalization.get ("id", {})
            equivalent_identifiers = normalization.get ("equivalent_identifiers", [])
            biolink_type = normalization.get ("type", [])

            """ Build the response. """
            if 'identifier' in preferred_id:
                logger.debug(f"Preferred id: {preferred_id}")
                variable['identifiers'][preferred_id['identifier']] = {
                    "label" : preferred_id.get('label',''),
                    "equivalent_identifiers" : [ v['identifier'] for v in equivalent_identifiers ],
                    "type" : biolink_type
                }
            else:
                variable['identifiers'][curie] = blank_preferred_id
                logger.debug (f"ERROR: normaliz({curie})=>({preferred_id}). No identifier?")
        except json.decoder.JSONDecodeError as e:
            variable['identifiers'][curie] = blank_preferred_id
            logger.error (f"JSONDecoderError normalizing curie: {curie}")

    def annotate (self, variables : Dict) -> Dict:
        """
        This operates on a dbGaP data dictionary which is
          an XML formatted study with a data_table root element containing a list of variables.
        - Tag prose variable descriptions with ontology identifiers.
        - Resolve those identifiers via Translator to preferred identifiers and BioLink Model categories.
        Specifically, for each variable
          create an object to represent the variable
          perform NLP annotation of the variable based on its description
            use the Monarch NLP annotator with named entity recognition
            (add additional NLP, eg. Vanderbilt and others here...?)
          normalize the resulting identifiers using the Translator normalization API

          :param variables: A dictionary of variables.
          :returns: A dictionary of annotted variables.
        """

        """
        Initialize and reuse a cached HTTP session for more efficient connection management.
        Use the Redis backend for requests-cache to store results accross executions
        """
        redis_connection = redis.StrictRedis (host=self.redis_host,
                                              port=self.redis_port,
                                              password=self.redis_password)
        http_session = CachedSession (
            cache_name='annotator',
            backend="redis",
            connection=redis_connection)


        variable_file = open("normalized_inputs.txt", "w")

        """ Annotate and normalize each variable. """
        for variable in variables:
            logger.debug (variable)
            try:
                """
                If the variable has an Xref identifier, normalize it.
                This data is only in the CSV formatted harmonized variable data.
                If that format goes away, delete this.
                """
                if 'xref' in variable:
                    self.normalize (http_session,
                                    variable['xref'],
                                    f"{self.normalizer}{variable['xref']}",
                                    variable)

                """ Annotate ontology terms in the text. """
                if not 'description' in variable:
                    logger.warn (f"this variable has no description: {json.dumps(variable, indent=2)}")
                    continue

                description = variable['description'].replace ("_", " ")
                description = self.debreviator.decode (description)
                encoded = urllib.parse.quote (description)
                url = f"{self.annotator}{encoded}"
                annotations = http_session.get(url).json ()


                """ Normalize each ontology identifier from the annotation. """
                for span in annotations.get('spans',[]):
                    for token in span.get('token',[]):
                        normalized = {}
                        curie = token.get('id', None)
                        if not curie:
                            continue
                        self.normalize (http_session,
                                        curie,
                                        f"{self.normalizer}{curie}",
                                        variable)

            except json.decoder.JSONDecodeError as e:
                traceback.print_exc ()
            except:
                traceback.print_exc ()
                raise
            variable_file.write(f"{json.dumps(variable, indent=2)}\n")
        return variables

    def write (self, graph : Dict) -> None:
        """
        Given a dictionary that is a graph containing nodes edges,
        use KGX to load the graph into a Neo4J database.

        :param graph: A KGX formatted graph as dictionary.
        """

        """ Load the knowledge graph into KGX and emit to Neo4J. """
        json_transformer = JsonTransformer ()
        json_transformer.load (graph)
        db = NeoTransformer (json_transformer.graph,
                             self.db_url,
                             self.username,
                             self.password)
        db.save()
        db.neo4j_report()

    def make_edge (self,
                   subj : str,
                   pred : str,
                   obj  : str,
                   edge_label : str ='association',
                   category : List[str] = []
    ):
        """
        Create an edge between two nodes.

        :param subj: The identifier of the subject.
        :param pred: The predicate linking the subject and object.
        :param obj: The object of the relation.
        :param edge_label: Label for the edge.
        :param category: The list of Biolink categories relating to the edge.
        :returns: Returns and edge.
        """
        edge_id = hashlib.md5(f'{subj}{pred}{obj}'.encode('utf-8')).hexdigest()
        return {
            "subject"     : subj,
            "predicate"   : pred,
            "id": edge_id,
            "edge_label"  : edge_label if len(edge_label) > 0 else "n/a",
            "object"      : obj,
            "provided_by" : "renci.bdc.semanticsearch.annotator",
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#type": "OBAN:association",
            "category" : category
        }

    def make_tagged_kg (self, variables : Dict, tags : Dict) -> Dict:
        """ Make a Translator standard knowledge graph representing
        tagged study variables.
        :param variables: The variables to model.
        :param tags: The tags characterizing the variables.
        :returns: Returns dictionary with nodes and edges modeling a Translator/Biolink KG.
        """
        graph = {
            "nodes" : [],
            "edges" : []
        }
        edges = graph['edges']
        nodes = graph['nodes']
        studies = {}
        tag_ref = {}

        """ Create graph elements to model tags and their
        links to identifiers gathered by semantic tagging. """
        tag_map = {}
        for tag in tags:
            tag_pk = tag['pk']
            tag_id  = tag['id']
            tag_map[tag_pk] = tag
            nodes.append ({
                "id" : tag_id,
                "pk" : tag_pk,
                "name" : tag['title'],
                "description" : tag['description'],
                "instructions" : tag['instructions'],
                "category" : [ "information_content_entity" ]
            })
            """ Link ontology identifiers we've found for this tag via nlp. """
            for identifier, metadata in tag['identifiers'].items ():
                nodes.append ({
                    "id" : identifier,
                    "name" : metadata['label'],
                    "category" : metadata['type']
                })
                edges.append (self.make_edge (
                    subj=tag_id,
                    pred="OBO:RO_0002434",
                    obj=identifier,
                    edge_label='association',
                    category=[ "association" ]))
                edges.append (self.make_edge (
                    subj=identifier,
                    pred="OBO:RO_0002434",
                    obj=tag_id,
                    edge_label='association',
                    category=[ "association" ]))

        """ Create nodes and edges to model variables, studies, and their
        relationships to tags. """
        for variable in variables:
            variable_id = variable['variable_id']
            variable_tag_pk = variable['tag_pk']
            study_id = variable['study_id']
            tag_id = tag_map[int(variable_tag_pk)]['id']
            if not study_id in studies:
                nodes.append ({
                    "id" : study_id,
                    "name" : variable['study_name'],
                    "category" : [ "clinical_trial" ]
                })
                studies[study_id] = study_id

            nodes.append ({
                "id" : variable_id,
                "name" : variable_id,
                "category" : [ "clinical_modifier" ]
            })
            """ Link to its study.  """
            edges.append (self.make_edge (
                subj=variable_id,
                edge_label='part_of',
                pred="OBO:RO_0002434",
                obj=study_id,
                category=['part_of']))
            edges.append (self.make_edge (
                subj=study_id,
                edge_label='has_part',
                pred="OBO:RO_0002434",
                obj=variable_id,
                category=['has_part']))

            """ Link to its tag. """
            edges.append (self.make_edge (
                subj=variable_id,
                edge_label='part_of',
                pred="OBO:RO_0002434",
                obj=tag_id,
                category=['part_of']))
            edges.append (self.make_edge (
                subj=tag_id,
                edge_label='has_part',
                pred="OBO:RO_0002434",
                obj=variable_id,
                category=['has_part']))

        return graph

    def convert_to_kgx_json (self, annotations):
        """
        Given an annotated and normalized set of study variables,
        generate a KGX compliant graph given the normalized annotations.
        Write that grpah to a graph database.
        See BioLink Model for category descriptions. https://biolink.github.io/biolink-model/notes.html
        """
        graph = {
            "nodes" : [],
            "edges" : []
        }
        edges = graph['edges']
        nodes = graph['nodes']

        for index, variable in enumerate(annotations):
            study_id = variable['study_id']
            if index == 0:
                """ assumes one study in this set. """
                nodes.append ({
                    "id" : study_id,
                    "category" : [ "clinical_trial" ]
                })

            """ connect the study and the variable. """
            edges.append (self.make_edge (
                subj=variable['variable_id'],
                edge_label='part_of',
                pred="OBO:RO_0002434",
                obj=study_id,
                category=['part_of']))
            edges.append (self.make_edge (
                subj=study_id,
                edge_label='has_part',
                pred="OBO:RO_0002434",
                obj=variable['variable_id'],
                category=['has_part']))

            """ a node for the variable. """
            nodes.append ({
                "id" : variable['variable_id'],
                "name" : variable['variable'],
                "description" : variable['description'],
                "category" : [ "clinical_modifier" ]
            })
            for identifier, metadata in variable['identifiers'].items ():
                edges.append (self.make_edge (
                    subj=variable['variable_id'],
                    pred="OBO:RO_0002434",
                    obj=identifier,
                    edge_label='association',
                    category=[ "case_to_phenotypic_feature_association" ]))
                edges.append (self.make_edge (
                    subj=identifier,
                    pred="OBO:RO_0002434",
                    obj=variable['variable_id'],
                    edge_label='association',
                    category=[ "case_to_phenotypic_feature_association" ]))
                nodes.append ({
                    "id" : identifier,
                    "name" : metadata['label'],
                    "category" : metadata['type']
                })
        return graph

    def get_variables_from_tranql(self):
        '''
        This function returns the tagged variables,
        including identifer information,
        from TranQL.  It is the starting point for
        indexing documents into ElasticSearch
        '''

        # Create a session and connect to TranQL
        tranql_endpoint = "https://tranql.renci.org/tranql/query?dynamic_id_resolution=true&asynchronous=false"
        headers = {
            "accept" : "application/json",
            "Content-Type" : "text/plain"
        }
        s = requests.Session()
        s.headers.update(headers)
        #TODO: Clean up the session object by bringing header, url, and data into attributes of obj

        # Build TranQL Query
        source = "/schema"
        questions = ["information_content_entity",
                     "clinical_modifier",
                     "clinical_trial"]
        query = f'select {"->".join(questions)} from "{source}"'
        single_query = query + f" where information_content_entity = 'TOPMED.TAG:51'"

        # Put this in while loop to iterate until we are successful in connecting to TranQL
        while True:
            try:
                response = s.post(url = tranql_endpoint,
                            data = query)
                if response.status_code is not 200:
                    logging.error(f"Encountered error: {response.status_code}:{response.reason}.  Trying again...")
                    continue
                break
            except requests.ConnectionError as e:
                logger.error (f"Encountered exception: {e}.  Trying again...")

        # Build List of Dicts
        response = response.json () # transform to JSON document if successful.
        nodes = response['knowledge_graph']['nodes']
        edges = response['knowledge_graph']['edges']
        tags = list(filter(lambda x: x['id'].startswith("TOPMED.TAG"),nodes))
        variables = list(filter(lambda x: x['id'].startswith("TOPMED.VAR"),nodes))
        studies = list(filter(lambda x: x['id'].startswith("TOPMED.STUDY"),nodes))

        # Annotate Tags w/ identifiers
        identifier_type = "biological_entity"
        
        for tag in tags:
            tag['title'] = tag.pop('name')
            # TranQL query to identifiers
            logger.debug(f"Querying Tag: {tag['title']} with query: {query}")
            query = f'select {identifier_type}->information_content_entity from "{source}" where information_content_entity = "{tag["id"]}"'
            response = s.post(url = tranql_endpoint,
                            data = query).json()
            logger.debug(response)
            identifiers = [identifier for identifier in response['knowledge_graph']['nodes'] if identifier['id'] != tag['id']]
            tag['identifiers'] = {}
            for identifier in identifiers:
                try:
                    # change 'name' to 'label' for downstream functionality.
                    identifier['label'] = identifier.pop('name') 
                except KeyError as e:
                    logger.error(f"{e}: Identifier does not have name/label.  Adding blank label")
                    identifier['label'] = ""
                tag['identifiers'][identifier['id']] = identifier
            '''
            tag_edges = [edge for edge in edges if edge['source_id']==tag['id'] and not edge['target_id'].startswith('TOPMED.VAR')]
            identifiers = [tag['target_id'] for tag in tag_edges]
            for identifier in identifiers:
                # Find identifers in node
                value = [node for node in nodes if node['id'] == identifier][0] # finds single dictionary
                if 'label' not in value:
                    value['label'] = value.pop('name') # change 'name' to 'label' for downstream processing
                tag['identifiers'][identifier] = value
            '''

        # Add studies and tag_pk to variables
        for variable in variables:
            # Rename
            variable['variable_id'] = variable.pop('id')

            # Grab from tags
            associations = [edge['source_id'] for edge in edges if edge['target_id']==variable['variable_id']]
            variable['tag_pk'] = next(filter(lambda x: x.startswith('TOPMED.TAG'), associations), "").split(":")[1]
            variable['study_id'] = next(filter(lambda x: x.startswith('TOPMED.STUDY'), associations), "")

            # Grab from studies - only linked to one study name
            variable['study_name'] = [study['name'] for study in studies if study['id']==variable['study_id']][0]

        # Return tags and variables
        return variables, tags

    def add_synonyms_to_identifiers(self, tags):
        '''
        This function does the following:
        - Initialize http_session for NCATS synonym service
        - list comprehension of all identifiers attached to tags
        - Add synonyms to all tags
        '''
        # Create an http_session like in annotate()
        redis_connection = redis.StrictRedis (host=self.redis_host,
                                              port=self.redis_port,
                                              password=self.redis_password)
        http_session = CachedSession (
            cache_name='annotator',
            backend="redis",
            connection=redis_connection)

        # Go through identifiers in tags
        for tag in tags:
            for identifier in list(tag['identifiers'].keys()):
                try:
                    # Get response from synonym service
                    encoded = urllib.parse.quote (identifier)
                    url = f"{self.synonym_service}{encoded}"
                    raw_synonyms = http_session.get(url).json ()

                    # List comprehension for synonyms
                    synonyms = [synonym['desc'] for synonym in raw_synonyms]
                    
                    # Add to tag
                    tag['identifiers'][identifier]['synonyms'] = synonyms
                
                except json.decoder.JSONDecodeError as e:
                    tag['identifiers'][identifier]['synonyms'] = []
                    logger.error (f"No synonyms returned for: {identifier}")
        return tags

class GraphDB:
    def __init__(self, conf):
        self.conf = conf
        self.gdb = GraphDatabase(url=conf['db_url'],
                                 username=conf['username'],
                                 password=conf['password'])
    def query (self, query):
        return self.gdb.query(query, data_contents=True)
