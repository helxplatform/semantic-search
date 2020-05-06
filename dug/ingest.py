import argparse
import csv
import json
import logging
import requests
import requests_cache
import os
import redis
import sys
import traceback
import urllib
import xml.etree.ElementTree as ET
import yaml
from dug.core import Search
from kgx import NeoTransformer, JsonTransformer
from neo4jrestclient.client import GraphDatabase
from neo4jrestclient import client
from requests_cache import CachedSession
from typing import List, Dict
import hashlib

logger = logging.getLogger (__name__)

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

Config = Dict

class Debreviator:
    """ Expand certain abbreviations to increase our hit rate. """
    def __init__(self):
        self.decoder = {
            "bmi" : "body mass index"
        }
    def decode (self, text):
        for key, value in self.decoder.items ():
            text = text.replace (key, value)
        return text

class TOPMedStudyAnnotator:
    """
    Annotate TOPMed study data with semantic knowledge graph linkages.

    """

    def __init__(self, config: Config):
        self.normalizer = config['normalizer']
        self.annotator = config['annotator']
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

        """ Normalize the identifier with respect to the BioLink Model. """
        try:
            normalized = http_session.get(url).json ()

            """ Record normalized results. """
            normalization = normalized.get(curie, {})
            preferred_id = normalization.get ("id", {})
            equivalent_identifiers = normalization.get ("equivalent_identifiers", [])
            biolink_type = normalization.get ("type", [])

            """ Build the response. """
            if 'identifier' in preferred_id:
                variable['identifiers'][preferred_id['identifier']] = {
                    "label" : preferred_id.get('label',''),
                    "equivalent_identifiers" : [ v['identifier'] for v in equivalent_identifiers ],
                    "type" : biolink_type
                }
            else:
                logger.debug (f"ERROR: normaliz({curie})=>({preferred_id}). No identifier?")
        except json.decoder.JSONDecodeError as e:
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
        db.save_with_unwind()
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
                "category" : [ "abstract_entity" ]
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

class GraphDB:
    def __init__(self, conf):
        self.conf = conf
        self.gdb = GraphDatabase(url=conf['db_url'],
                                 username=conf['username'],
                                 password=conf['password'])
    def query (self, query):
        return self.gdb.query(query, data_contents=True)

def main ():
    """
    Configure an annotator.
    """
    db_url_default = "http://" + os.environ.get('NEO4J_HOST', 'localhost') + ":" + os.environ.get('NEO4J_PORT', '7474') + "/db/data"
    config = {
        'annotator'      : "https://api.monarchinitiative.org/api/nlp/annotate/entities?min_length=4&longest_only=false&include_abbreviation=false&include_acronym=false&include_numbers=false&content=",
        'normalizer'     : "https://nodenormalization-sri.renci.org/get_normalized_nodes?curie=",
        'password'       : os.environ['NEO4J_PASSWORD'],
        'username'       : 'neo4j',
        'db_url'         : db_url_default,
        'redis_host'     : os.environ.get('REDIS_HOST', 'localhost'),
        'redis_port'     : os.environ.get('REDIS_PORT', 6379),
        'redis_password' : os.environ.get('REDIS_PASSWORD', ''),
    }

    parser = argparse.ArgumentParser(description='Load edges and nodes into Neo4j via kgx')
    parser.add_argument('--load', help='annotation file to load via kgx')
    parser.add_argument('--annotate', help='annotate TOPMed data dictionary file', default=None)
    parser.add_argument('--db-url', help='database url', default=db_url_default)
    parser.add_argument('--db-username', help='database username', default='neo4j')
    parser.add_argument('--db-password', help='database password', default=os.environ['NEO4J_PASSWORD'])
    parser.add_argument('--tagged', help='annotate tagged variables')
    parser.add_argument('--index', help='build search index directly. valid only with --tagged.', default=False)
    parser.add_argument('--debug', help='debug output', default=False)
    args = parser.parse_args()

    config['username'] = args.db_username
    config['password'] = args.db_password
    config['db_url']   = args.db_url
    annotator = TOPMedStudyAnnotator (config=config)

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    if args.load:
        """ Load an annotated data dictionary and write to a graph database. """
        with open(args.load, 'r') as stream:
            obj = json.load(stream)

            """ Convert. """
            knowledge_graph = annotator.convert_to_kgx_json (obj)

            """ Write output """
            annotator.write (knowledge_graph)
    elif args.annotate:

        input_file = args.annotate
        variables = annotator.load_data_dictionary (input_file) \
                    if input_file.endswith('.xml') \
                       else annotator.load_csv (input_file)

        """ Annotate an input file using the MonarchInitiative SciGraph named entity extractor and annotator. """
        response = annotator.annotate (variables)

        """ Write the annotated variables. """
        output_file = args.annotate.\
                      replace ('.xml', '_tagged.json').\
                      replace ('.csv', '_tagged.json')
        with open(output_file, 'w') as stream:
            json.dump(response, stream, indent=2)
    elif args.tagged:

        """
        The link operator for tagged variables.
        This annotates, normalizes, converts to a kgs graph, and loads via KGX into Neo4J.
        """
        variables, tags = annotator.load_tagged_variables (args.tagged)
        tags = annotator.annotate (tags)
        logger.debug (f"{json.dumps(tags, indent=2)}")
        knowledge_graph = annotator.make_tagged_kg (variables, tags)
        logger.debug (f"{json.dumps(knowledge_graph, indent=2)}")
        annotator.write (knowledge_graph)

        if (args.index):
            """ PHASE-1: Go directly to Elasticsearch, bypassing Translator. """

            """ Initialize the search engine core. """
            search = Search ()

            """ Connect to the graph database. """
            graph = GraphDB (config)

            """
            For each Biolink model category, get subgraphs initiated with the category, connected to a tag, to a variable, to a study.
            For each resulting subgraph, build a document linking data from each of these layers.
            Add the document to the search index.
            """
            for category in [ "phenotypic_feature", "anatomical_entity", "cell_type", "gene", "disease", "biological_process" ]:
                page_size= 10_000
                path_count_cypher = f"""MATCH 
                    p=(bioconcept:{category})--(tag:abstract_entity)--(variable:clinical_modifier)--(study:clinical_trial) 
                    RETURN count(p) as path_count
                """
                get_path_query = lambda skip, limit: f"MATCH" \
                    f"(bioconcept:{category})--(tag:abstract_entity)--(variable:clinical_modifier)--(study:clinical_trial)" \
                    f"RETURN * SKIP {skip} LIMIT {limit}"
                path_count = graph.query(path_count_cypher)[0][0]
                logger.debug(f"Found {path_count} paths for ({category})--(abstract_entity)"
                             "--(clinical_modifier)--(clinical_trial)")
                for offset in range(0, path_count, page_size):
                    skip = offset
                    limit = skip + page_size
                    query = get_path_query(skip, limit)
                    result = graph.query (query)
                    if result:
                        for index, row in enumerate(result.rows):
                            """
                            We're looking at a single subgraph returned from the query.
                            Build a document collecting these artifacts.
                            """
                            text = []
                            doc = {}
                            for node in row:
                                node_id = node['id']
                                if node_id.startswith ("TOPMED.VAR:"):
                                    doc['var'] = node_id.replace  ("TOPMED.VAR:", "")
                                elif node_id.startswith ("TOPMED.STUDY"):
                                    doc['study'] = node_id.replace ("TOPMED.STUDY:", "")
                                    text.append (node['name'])
                                elif node_id.startswith ("TOPMED.TAG"):
                                    doc['tag'] = node_id.replace ("TOPMED.TAG:", "")
                                    text.append (node['description'])
                                    text.append (node['instructions'])
                                else:
                                    doc['id'] = node_id
                                    text.append (node['name'])

                            doc['name'] = text
                            logger.debug (f"{json.dumps(doc, indent=2)}")

                            """ Index the document. """
                            search.index_doc (
                                index='test',
                                doc=doc,
                                doc_id=doc['id'])

if __name__ == '__main__':
    main ()
