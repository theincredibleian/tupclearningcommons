-- phpMyAdmin SQL Dump
-- version 5.2.1
-- https://www.phpmyadmin.net/
--
-- Host: 127.0.0.1
-- Generation Time: Jul 17, 2026 at 01:32 PM
-- Server version: 10.4.32-MariaDB
-- PHP Version: 8.2.12

SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
SET time_zone = "+00:00";


/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;

--
-- Database: `learning_commons_db`
--
CREATE DATABASE IF NOT EXISTS `learning_commons_db` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE `learning_commons_db`;

-- --------------------------------------------------------

--
-- Table structure for table `appointments`
--

CREATE TABLE `appointments` (
  `id` int(11) NOT NULL,
  `id_number` varchar(30) NOT NULL,
  `first_name` varchar(100) NOT NULL,
  `last_name` varchar(100) NOT NULL,
  `mi` varchar(5) DEFAULT NULL,
  `gsfe_email` varchar(150) NOT NULL,
  `date` date NOT NULL,
  `station_no` varchar(50) NOT NULL,
  `location` varchar(100) NOT NULL,
  `start_time` time NOT NULL,
  `end_time` time NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT current_timestamp(),
  `status` varchar(20) NOT NULL DEFAULT 'reserved'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

--
-- Dumping data for table `appointments`
--

INSERT INTO `appointments` (`id`, `id_number`, `first_name`, `last_name`, `mi`, `gsfe_email`, `date`, `station_no`, `location`, `start_time`, `end_time`, `created_at`, `status`) VALUES
(3, 'LC-07-12-2026-0001', 'Jian Ray', 'Lomibao', 'I.', 'jiansmediaverse@gmail.com', '2026-07-13', 'Station 2', 'Main Learning Commons', '08:00:00', '10:00:00', '2026-07-12 00:34:38', 'expired'),
(4, 'LC-07-12-2026-0002', 'Jian Ray', 'Lomibao', 'I.', 'jiansmediaverse@gmail.com', '2026-07-13', 'Station 6', 'Main Learning Commons', '12:00:00', '13:00:00', '2026-07-12 01:06:20', 'expired'),
(5, 'LC-07-13-2026-0001', 'Jian Ray', 'Lomibao', 'I.', 'jiansmediaverse@gmail.com', '2026-07-13', 'Station 3', 'Main Learning Commons', '12:00:00', '14:00:00', '2026-07-13 04:15:56', 'expired'),
(6, 'LC-07-13-2026-0002', 'Jian Ray', 'Lomibao', 'I.', 'jiansmediaverse@gmail.com', '2026-07-14', 'Station 2', 'Main Learning Commons', '08:00:00', '10:00:00', '2026-07-13 11:41:07', 'expired'),
(7, 'LC-07-13-2026-0003', 'Jian Ray', 'Lomibao', 'I.', 'jiansmediaverse@gmail.com', '2026-07-14', 'Station 2', 'Main Learning Commons', '08:00:00', '10:00:00', '2026-07-13 11:43:03', 'expired'),
(11, 'LC-07-14-2026-0001', 'Jian Ray', 'Lomibao', 'I.', 'jianraylomibao.official@gmail.com', '2026-07-14', 'Station 4', 'Main Learning Commons', '12:30:00', '13:00:00', '2026-07-14 04:28:23', 'completed'),
(12, 'LC-07-15-2026-0001', 'Jian Ray', 'Lomibao', 'I.', 'jianraylomibao.official@gmail.com', '2026-07-15', 'Station 1', 'Main Learning Commons', '11:30:00', '12:30:00', '2026-07-15 03:33:35', 'completed'),
(13, 'LC-07-17-2026-0001', 'Jian Ray', 'Lomibao', 'I', 'jianraylomibao.official@gmail.com', '2026-07-17', 'Station 1', 'Main Learning Commons', '14:30:00', '15:00:00', '2026-07-17 06:27:40', 'completed');

-- --------------------------------------------------------

--
-- Table structure for table `locations`
--

CREATE TABLE `locations` (
  `id` int(11) NOT NULL,
  `name` varchar(100) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

--
-- Dumping data for table `locations`
--

INSERT INTO `locations` (`id`, `name`) VALUES
(1, 'Main Learning Commons');

-- --------------------------------------------------------

--
-- Table structure for table `restrictions`
--

CREATE TABLE `restrictions` (
  `id` int(11) NOT NULL,
  `first_name` varchar(100) DEFAULT NULL,
  `last_name` varchar(100) DEFAULT NULL,
  `mi` varchar(10) DEFAULT NULL,
  `gsfe_email` varchar(150) NOT NULL,
  `restricted_at` datetime NOT NULL,
  `restricted_until` datetime NOT NULL,
  `active` tinyint(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

--
-- Dumping data for table `restrictions`
--

INSERT INTO `restrictions` (`id`, `first_name`, `last_name`, `mi`, `gsfe_email`, `restricted_at`, `restricted_until`, `active`) VALUES
(1, 'Jian Ray', 'Lomibao', 'I.', 'jiansmediaverse@gmail.com', '2026-07-15 11:31:33', '2026-08-14 11:31:33', 1);

-- --------------------------------------------------------

--
-- Table structure for table `stations`
--

CREATE TABLE `stations` (
  `id` int(11) NOT NULL,
  `station_no` varchar(50) NOT NULL,
  `location` varchar(100) NOT NULL,
  `is_closed` tinyint(1) NOT NULL DEFAULT 0,
  `sort_order` int(11) NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

--
-- Dumping data for table `stations`
--

INSERT INTO `stations` (`id`, `station_no`, `location`, `is_closed`, `sort_order`) VALUES
(1, 'Station 1', 'Main Learning Commons', 0, 1),
(2, 'Station 2', 'Main Learning Commons', 0, 2),
(3, 'Station 3', 'Main Learning Commons', 0, 3),
(4, 'Station 4', 'Main Learning Commons', 0, 4),
(5, 'Station 5', 'Main Learning Commons', 0, 5),
(6, 'Station 6', 'Main Learning Commons', 0, 6),
(7, 'Station 7', 'Main Learning Commons', 0, 7),
(8, 'Station 8', 'Main Learning Commons', 0, 8),
(9, 'Station 9', 'Main Learning Commons', 0, 9),
(10, 'Station 10', 'Main Learning Commons', 0, 10);

--
-- Indexes for dumped tables
--

--
-- Indexes for table `appointments`
--
ALTER TABLE `appointments`
  ADD PRIMARY KEY (`id`),
  ADD UNIQUE KEY `id_number` (`id_number`),
  ADD KEY `idx_id_number` (`id_number`);

--
-- Indexes for table `locations`
--
ALTER TABLE `locations`
  ADD PRIMARY KEY (`id`),
  ADD UNIQUE KEY `name` (`name`);

--
-- Indexes for table `restrictions`
--
ALTER TABLE `restrictions`
  ADD PRIMARY KEY (`id`),
  ADD UNIQUE KEY `uniq_gsfe_email` (`gsfe_email`);

--
-- Indexes for table `stations`
--
ALTER TABLE `stations`
  ADD PRIMARY KEY (`id`),
  ADD UNIQUE KEY `uniq_station_location` (`station_no`,`location`);

--
-- AUTO_INCREMENT for dumped tables
--

--
-- AUTO_INCREMENT for table `appointments`
--
ALTER TABLE `appointments`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT, AUTO_INCREMENT=14;

--
-- AUTO_INCREMENT for table `locations`
--
ALTER TABLE `locations`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT, AUTO_INCREMENT=3;

--
-- AUTO_INCREMENT for table `restrictions`
--
ALTER TABLE `restrictions`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT, AUTO_INCREMENT=2;

--
-- AUTO_INCREMENT for table `stations`
--
ALTER TABLE `stations`
  MODIFY `id` int(11) NOT NULL AUTO_INCREMENT, AUTO_INCREMENT=24;
COMMIT;

/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;