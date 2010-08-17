import urllib2
import time

from ckan import model
from ckan.lib.create_test_data import CreateTestData
from ckan.tests import *
from ckan.tests.wsgi_ckanclient import WsgiCkanClient
from ckanclient import CkanClient
from ckanext.getdata.loader import PackageLoader

USER = u'annafan'

# Set to true for quicker tests using wsgi_ckanclient
# otherwise it uses ckanclient
WSGI_CLIENT = True

def count_pkgs():
    return model.Session.query(model.Package).count()

class LoaderBase(TestController):
    def setup(self):
        CreateTestData.create_arbitrary([], extra_user_names=[USER])
        user = model.User.by_name(USER)
        assert user
        if WSGI_CLIENT:
            self.testclient = WsgiCkanClient(self.app, api_key=user.apikey)
        else:
            self.sub_proc = self._start_ckan_server('test.ini')
            self.testclient = CkanClient(base_location='http://localhost:5000/api',
                                         api_key=user.apikey)
            self._wait_for_url(url='http://localhost:5000/api')


    def teardown(self):
        if WSGI_CLIENT:
            CreateTestData.delete()
        else:
            try:
                self._stop_ckan_server(self.sub_proc)
            finally:
                CreateTestData.delete()        


class TestLoader(LoaderBase):
    def setup(self):
        super(TestLoader, self).setup()
        self.loader = PackageLoader(self.testclient)

    # teardown is in the base class

    def test_0_simple_load(self):
        pkg_dict = {'name':u'pkgname',
                    'title':u'Boris'}
        assert not model.Package.by_name(pkg_dict['name'])
        CreateTestData.flag_for_deletion(pkg_names=[pkg_dict['name']])
        self.loader.load_package(pkg_dict)
        pkg = model.Package.by_name(pkg_dict['name'])
        assert pkg
        assert pkg.name == pkg_dict['name']
        assert pkg.title == pkg_dict['title']

    def test_1_load_several(self):
        num_pkgs = count_pkgs()
        pkg_dicts = [{'name':u'pkgname_a',
                      'title':u'BorisA'},
                     {'name':u'pkgname_b',
                      'title':u'BorisB'},
                     ]
        assert not model.Package.by_name(pkg_dicts[0]['name'])
        CreateTestData.flag_for_deletion(pkg_names=[pkg_dict['name'] for pkg_dict in pkg_dicts])
        num_loaded, num_errors = self.loader.load_packages(pkg_dicts)
        assert (num_loaded, num_errors) == (2, 0), (num_loaded, num_errors)
        assert count_pkgs() == num_pkgs + 2, (count_pkgs() - num_pkgs)

    def test_1_load_several_with_errors(self):
        num_pkgs = count_pkgs()
        pkg_dicts = [{'name':u'pkgnameA', # not allowed uppercase name
                      'title':u'BorisA'},
                     {'name':u'pkgnameB',
                      'title':u'BorisB'},
                     ]
        assert not model.Package.by_name(pkg_dicts[0]['name'])
        CreateTestData.flag_for_deletion(pkg_names=[pkg_dict['name'] for pkg_dict in pkg_dicts])
        num_loaded, num_errors = self.loader.load_packages(pkg_dicts)
        assert (num_loaded, num_errors) == (0, 2), (num_loaded, num_errors)
        assert count_pkgs() == num_pkgs, (count_pkgs() - num_pkgs)

    def test_2_reload(self):
        # load the package once
        num_pkgs = count_pkgs()
        pkg_dict = {'name':u'pkgname2',
                    'title':u'Boris'}
        assert not model.Package.by_name(pkg_dict['name'])
        CreateTestData.flag_for_deletion(pkg_names=[pkg_dict['name']])
        self.loader.load_package(pkg_dict)
        pkg = model.Package.by_name(pkg_dict['name'])
        assert pkg
        assert count_pkgs() == num_pkgs + 1, (count_pkgs() - num_pkgs)

        # load the package again
        pkg_dict = {'name':u'pkgname2',
                    'title':u'Boris Becker'}
        self.loader.load_package(pkg_dict)
        pkg = model.Package.by_name(pkg_dict['name'])
        assert pkg
        assert pkg.name == pkg_dict['name']
        assert pkg.title == pkg_dict['title'], pkg.title
        assert count_pkgs() == num_pkgs + 1, (count_pkgs() - num_pkgs)

class TestLoaderUsingUniqueFields(LoaderBase):
    def setup(self):
        self.tsi = TestSearchIndexer()
        super(TestLoaderUsingUniqueFields, self).setup()
        self.loader = PackageLoader(self.testclient, unique_extra_field='ref')

    # teardown is in the base class

    def test_0_reload(self):
        # create initial package
        num_pkgs = count_pkgs()
        pkg_dict = {'name':u'pkgname0',
                    'title':u'Boris',
                    'extras':{u'ref':'boris'}}
        assert not model.Package.by_name(pkg_dict['name'])
        CreateTestData.create_arbitrary([pkg_dict])
        self.tsi.index()
        pkg = model.Package.by_name(pkg_dict['name'])
        assert pkg
        assert count_pkgs() == num_pkgs + 1, (count_pkgs() - num_pkgs)

        # load the package with same name and ref
        pkg_dict = {'name':u'pkgname0',
                    'title':u'Boris 2',
                    'extras':{u'ref':'boris'}}
        self.loader.load_package(pkg_dict)
        pkg = model.Package.by_name(pkg_dict['name'])
        assert pkg
        assert pkg.name == pkg_dict['name']
        assert pkg.title == pkg_dict['title']
        assert count_pkgs() == num_pkgs + 1, (count_pkgs() - num_pkgs)

        # load the package with different name, same ref
        pkg_dict = {'name':u'pkgname0changed',
                    'title':u'Boris 3',
                    'extras':{u'ref':'boris'}}
        CreateTestData.flag_for_deletion(pkg_names=[pkg_dict['name']])
        self.loader.load_package(pkg_dict)
        pkg = model.Package.by_name(pkg_dict['name'])
        assert pkg
        assert pkg.name == pkg_dict['name']
        assert pkg.title == pkg_dict['title']
        assert count_pkgs() == num_pkgs + 1, (count_pkgs() - num_pkgs)

        # load the package with same name, different ref - new package
        other_pkg_dict = pkg_dict
        pkg_dict = {'name':u'pkgname0changed',
                    'title':u'Boris 4',
                    'extras':{u'ref':'boris-4'}}
        CreateTestData.flag_for_deletion(pkg_names=[pkg_dict['name']])
        self.loader.load_package(pkg_dict)
        assert pkg_dict['name'] == 'pkgname0changed_'
        orig_pkg = model.Package.by_name(u'pkgname0changed')
        assert orig_pkg
        assert orig_pkg.title == u'Boris 3'
        pkg = model.Package.by_name(pkg_dict['name'])
        assert pkg
        assert pkg.name == pkg_dict['name']
        assert pkg.title == pkg_dict['title']
        assert model.Package.by_name(other_pkg_dict['name'])
        assert count_pkgs() == num_pkgs + 2, (count_pkgs() - num_pkgs)

        
class TestLoaderNoSearch(LoaderBase):
    '''Cope as best as possible if search indexing is flakey.'''
    def setup(self):
        '''NB, no search indexing started'''
        super(TestLoaderNoSearch, self).setup()
        self.loader = PackageLoader(self.testclient, unique_extra_field='ref')

    # teardown is in the base class

    def test_0_reload(self):
        # create initial package
        num_pkgs = count_pkgs()
        pkg_dict = {'name':u'pkgname0',
                    'title':u'Boris',
                    'extras':{u'ref':'boris'}}
        assert not model.Package.by_name(pkg_dict['name'])
        CreateTestData.create_arbitrary([pkg_dict])
        pkg = model.Package.by_name(pkg_dict['name'])
        assert pkg
        assert count_pkgs() == num_pkgs + 1, (count_pkgs() - num_pkgs)

        # load the package with same name and ref
        pkg_dict = {'name':u'pkgname0',
                    'title':u'Boris 2',
                    'extras':{u'ref':'boris'}}
        self.loader.load_package(pkg_dict)
        pkg = model.Package.by_name(pkg_dict['name'])
        assert pkg
        assert pkg.name == pkg_dict['name']
        assert pkg.title == pkg_dict['title']
        assert count_pkgs() == num_pkgs + 1, (count_pkgs() - num_pkgs)
        # i.e. not tempted to create pkgname0_ alongside pkgname0
        

##class TestLoaderCkanClient(LoaderBase):
##    '''Runs simple test using ckanclient itself'''
##    def setup(self):
##        self.sub_proc = self._start_ckan_server('test.ini')
##        CreateTestData.create_arbitrary([], extra_user_names=[USER])
##        user = model.User.by_name(USER)
##        assert user
##        super(TestLoaderNoSearch, self).setup()
##        self.testclient = CkanClient(base_location='http://localhost:5000/api',
##                                     api_key=user.apikey)
##        self.loader = PackageLoader(self.testclient)

##    def teardown(self):
##        try:
##            self._stop_ckan_server(self.sub_proc)
##        finally:
##            CreateTestData.delete()        
    
##    def test_0_simple_load(self):
##        pkg_dict = {'name':u'pkgname',
##                    'title':u'Boris'}
##        assert not model.Package.by_name(pkg_dict['name'])
##        self._wait_for_url(url='http://localhost:5000/api')
##        pkg = self.testclient.package_entity_get(pkg_dict['name'])
##        assert self.testclient.last_status == 404, self.testclient.last_status
##        CreateTestData.flag_for_deletion(pkg_names=[pkg_dict['name']])
##        self.loader.load_package(pkg_dict)
##        pkg = self.testclient.package_entity_get(pkg_dict['name'])
##        assert self.testclient.last_status == 200
##        assert pkg
##        assert pkg['name'] == pkg_dict['name']
##        assert pkg['title'] == pkg_dict['title']
