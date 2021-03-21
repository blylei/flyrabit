-- create a user to connected DB server
CREATE USER 'frabit'@'localhost' IDENTIFIED BY 'Frabit@123';
GRANT INSERT, SELECT, UPDATE, DELETE ON frabit.* TO 'frabit'@'localhost';

-- create frabit database used store backup policy and relevent info
CREATE DATABASE frabit /*!40100 DEFAULT CHARACTER SET utf8mb4 */;

-- create tables within frabit
CREATE TABLE frabit.instance_list(
 `id` int not null auto_increment comment '自增ID'
,`host` varchar(15) not null comment '待备份实例'
,`port` smallint not null default 3306 comment '待备份实例监听端口'
,`remark` varchar(500) not null default '' comment '对备份实例的简短介绍'
primary key (`id`)
index 'idx_host_port' (`host`,`port`)
) engine=InnoDB charset = utf8mb4;

CREATE TABLE frabit.backup_task(
 `id` int not null auto_increment comment '自增ID'
,`host` varchar(15) not null comment '待备份实例'
,`port` smallint not null default 3306 comment '待备份实例监听端口'
,`remark` varchar(500) not null default '' comment '对备份实例的简短介绍'
primary key (`id`)
index 'idx_host_port' (`host`,`port`)
) engine=InnoDB charset = utf8mb4;

CREATE TABLE frabit.recovery_task(
 `id` int not null auto_increment comment '自增ID'
,`host` varchar(15) not null comment '待备份实例'
,`port` smallint not null default 3306 comment '待备份实例监听端口'
,`remark` varchar(500) not null default '' comment '对备份实例的简短介绍'
primary key (`id`)
index 'idx_host_port' (`host`,`port`)
) engine=InnoDB charset = utf8mb4;


