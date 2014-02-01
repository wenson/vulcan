#!/usr/bin/env python
# coding:utf-8
# code by pnig0s@20140131

'''
Vulcan Spider @ 2014

基于gevent和多线程模型，支持WebKit引擎的DOM解析动态爬虫框架。
框架由两部分组成：
fetcher:下载器，负责获取HTML，送入crawler。
crawler:爬取器，负责解析并爬取HTML中的URL，送入fetcher。
fetcher和crawler两部分独立工作，互不干扰，通过queue进行链接
fetcher需要发送HTTP请求，涉及到阻塞操作，使用gevent池控制
crawler没有涉及阻塞操作，但为了扩展可以自选gevent池和多线程池两种模型控制

'''

import gevent
from gevent import monkey
from gevent import Greenlet
from gevent import pool
from gevent import queue
from gevent import event
from gevent import Timeout

from lxml import etree
from exceptions import *

import threadpool
monkey.patch_all()
import time
import uuid
import urllib2
import string
import urlparse
import logging
import random

import lxml.html as H
import chardet

from Data import UrlCache
from Data import UrlData

class HtmlAnalyzer(object):
    '''页面分析类'''
    @staticmethod
    def extract_links(html,base_ref,tags=[]):
        '''
        抓取页面内链接(生成器)
        base_ref : 用于将页面中的相对地址转换为绝对地址
        tags     : 期望从该列表所指明的标签中提取链接
        '''
        if not html.strip():
            return
            
        link_list = []
        try:
            doc = H.document_fromstring(html)
        except Exception,e:
            return
            
        default_tags = ['a','img','iframe','frame']
        default_tags.extend(tags)
        default_tags = list(set(default_tags))
        doc.make_links_absolute(base_ref)
        links_in_doc = doc.iterlinks()
        for link in links_in_doc:
            if link[0].tag in set(default_tags):
                yield link[2]
                

class Fetcher(Greenlet):
    """抓取器(下载器)类"""
    def __init__(self,spider):
        Greenlet.__init__(self)
        self.fetcher_id = str(uuid.uuid1())[:8]
        self.spider = spider
        self.fetcher_cache = self.spider.fetcher_cache
        self.crawler_cache = self.spider.crawler_cache
        self.fetcher_queue = self.spider.fetcher_queue
        self.crawler_queue = self.spider.crawler_queue
        self.logger = self.spider.logger
        
    def _fetcher(self):
        '''
        抓取器主函数
        '''
        self.logger.info("fetcher %s starting...." % (self.fetcher_id,))
        while not self.spider.stopped.isSet():
            try:
                url_data = self.fetcher_queue.get(block=False)
            except queue.Empty,e:
                if self.crawler_queue.unfinished_tasks == 0 and self.fetcher_queue.unfinished_tasks == 0:
                    self.spider.stop()
                else:
                    gevent.sleep()
            else:
                if not url_data.html:
                    try:
                        if url_data not in self.crawler_cache:
                            self.logger.info("fetcher %s accept %s" % (self.fetcher_id,url_data))
                            html = self._open(url_data)
                            url_data.html = html
                            self.crawler_queue.put(url_data,block=True)
                            self.crawler_cache.insert(url_data)
                    except Exception,e:
                        pass
                self.spider.fetcher_queue.task_done()

    def _open(self,url):
        '''
        获取HTML内容
        '''
        try:
            req = urllib2.Request(str(url))
            resp = urllib2.urlopen(req)
        except (urllib2.URLError,urllib2.HTTPError),e:
            self.logger.warn("%s %s" % (self.url,str(e)))
            return ''
        else:
            html = resp.read()
            resp.close()
            return html
    
    def _run(self):
        self._fetcher()
        

class Spider(object):
    """爬虫主类"""
    logger = logging.getLogger("spider")
        
    def __init__(self, concurrent_num=20, crawl_tags=[], depth=3, 
                 max_url_num=300, internal_timeout=60, spider_timeout=6*3600, crawler_mode=0, same_origin=True):
        """
        concurrent_num    : 并行crawler和fetcher数量
        crawl_tags        : 爬行时收集URL所属标签列表
        depth             : 爬行深度限制
        max_url_num       : 最大收集URL数量
        internal_timeout  : 内部调用超时时间
        spider_timeout    : 爬虫超时时间
        crawler_mode      : 爬取器模型(0:多线程模型,1:gevent模型)
        same_origin       : 是否限制相同域下
        """
        
        self.logger.setLevel(logging.DEBUG)
        hd = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        hd.setFormatter(formatter)
        self.logger.addHandler(hd)
        
        self.stopped = event.Event()
        self.internal_timer = Timeout(internal_timeout)
        
        self.crawler_mode = crawler_mode #爬取器模型
        self.concurrent_num = concurrent_num
        self.fetcher_pool = pool.Pool(self.concurrent_num)
        if self.crawler_mode == 0:
            self.crawler_pool = threadpool.ThreadPool(min(50,self.concurrent_num))
        else:
            self.crawler_pool = pool.Pool(self.concurrent_num)
        self.fetcher_queue = queue.JoinableQueue(maxsize=self.concurrent_num*100)
        self.crawler_queue = queue.JoinableQueue(maxsize=self.concurrent_num*100)
        self.fetcher_cache = UrlCache()
        self.crawler_cache = UrlCache()
        
        self.default_crawl_tags = ['a','base','iframe','frame','object']
        self.ignore_ext = ['js','css','png','jpg','gif','bmp','svg','exif','jpeg','exe','rar','zip']
        self.crawl_tags = list(set(self.default_crawl_tags)|set(crawl_tags))
        self.same_origin = same_origin
        self.depth = depth
        self.max_url_num = max_url_num
    
    def _start_fetcher(self):
        '''
        启动下载器
        '''
        for i in xrange(self.concurrent_num):
            fetcher = Fetcher(self)
            self.fetcher_pool.start(fetcher)
    
    def _start_crawler(self):
        '''
        启动爬取器
        '''
        if self.crawler_mode == 0:
            '''多线程模型'''
            reqs = threadpool.makeRequests(self.crawler,xrange(self.concurrent_num))
            for req in reqs:
                self.crawler_pool.putRequest(req)
            self.crawler_pool.wait()
        else:
            '''Gevent模型'''
            for j in xrange(self.concurrent_num):
                self.crawler_pool.spawn(self.crawler)

    def start(self):
        '''
        主启动函数
        '''
        self.logger.info("spider starting...")
        if self.crawler_mode == 0:
            self.logger.info("crawler run in multi-thread mode.")
        elif self.crawler_mode == 1:
            self.logger.info("crawler run in gevent mode.")
            
        self._start_fetcher()
        self._start_crawler()
        
        self.stopped.wait() #等待停止事件置位
        
        try:
            self.internal_timer.start()
            self.fetcher_pool.join(timeout=self.internal_timer)
            if self.crawler_mode == 1:
                self.crawler_pool.join(timeout=self.internal_timer)
        except Timeout:
            self.logger.error("internal timeout triggered")
        finally:
            self.internal_timer.cancel()
            
        self.stopped.clear()
        self.logger.info("spider process quit.")
    
    def crawler(self,_dep=None):
        '''
        爬行器主函数
        '''
        while not self.stopped.isSet():
            try:
                self._maintain_spider() #维护爬虫池
                url_data = self.crawler_queue.get(block=False)
            except queue.Empty,e:
                if self.crawler_queue.unfinished_tasks == 0 and self.fetcher_queue.unfinished_tasks == 0:
                    self.stop()
                else:
                    gevent.sleep()
            else:
                pre_depth = url_data.depth
                curr_depth = pre_depth+1
                link_generator = HtmlAnalyzer.extract_links(url_data.html,url_data.url,self.crawl_tags)
                for index,link in enumerate(link_generator):
                    if link in self.fetcher_cache:
                        continue
                        
                    if not link.startswith("http"):
                        continue
                    
                    if self.same_origin:
                        if not self._check_same_origin(link):
                            continue
                    
                    if curr_depth > self.depth:   #最大爬行深度判断
                        continue
                    
                    if len(self.fetcher_cache) == self.max_url_num:   #最大收集URL数量判断
                        continue
                    
                    link = self._to_unicode(link)
                    url = UrlData(link,depth=curr_depth)
                    self.fetcher_cache.insert(url)
                    self.fetcher_queue.put(url,block=True)
                self.crawler_queue.task_done()
                                    
    def feed_url(self,url):
        '''
        设置初始爬取URL
        '''
        if isinstance(url,basestring):
            url = self._to_unicode(url)
            url = UrlData(url)
            
        if self.same_origin:
            url_part = urlparse.urlparse(unicode(url))
            self.origin = (url_part.scheme,url_part.netloc)
            
        self.fetcher_queue.put(url,block=True)
        
    def stop(self):
        '''
        终止爬虫
        '''
        self.stopped.set()
    
    def _to_unicode(self,data):
        '''
        将输入的字符串转化为unicode对象
        '''
        unicode_data = ''
        if isinstance(data,str):
            charset = chardet.detect(data).get('encoding')
            if charset:
                unicode_data = data.decode(charset)
            else:
                unicode_data = data
        else:
            unicode_data = data
        return unicode_data
        
    def _maintain_spider(self):
        '''
        维护爬虫池:
        1)从池中剔除死掉的crawler和fetcher
        2)根据剩余任务数量及池的大小补充crawler和fetcher
        维持爬虫池饱满
        '''
        if self.crawler_mode == 1:
            for greenlet in list(self.crawler_pool):
                if greenlet.dead:
                    self.crawler_pool.discard(greenlet)
            for i in xrange(min(self.crawler_queue.qsize(),self.crawler_pool.free_count())):
                self.crawler_pool.spawn(self.crawler)
        
        for greenlet in list(self.fetcher_pool):
            if greenlet.dead:
                self.fetcher_pool.discard(greenlet)
        for i in xrange(min(self.fetcher_queue.qsize(),self.fetcher_pool.free_count())):
            fetcher = Fetcher(self)
            self.fetcher_pool.start(fetcher)
    
    def _check_same_origin(self,current_url):
        '''
        检查两个URL是否同源
        '''
        current_url = self._to_unicode(current_url)
        url_part = urlparse.urlparse(current_url)
        url_origin = (url_part.scheme,url_part.netloc)
        return url_origin == self.origin

if __name__ == '__main__':
    spider = Spider(concurrent_num=20,depth=3,max_url_num=300,crawler_mode=0)
    spider.feed_url("http://www.freebuf.com/")
    spider.start()