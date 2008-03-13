/*
 * Copyright (C) 2005, 2006, 2007, 2008 Thomas Cort <tom@tomcort.com>
 *
 * This file is part of libtinypoker.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 */

#include "../main/tinypoker.h"
#include "test.h"

int main()
{
	assertTrue("Yes String should be valid", ipp_validate_msg(REGEX_MSG_YES, "YES STRAIGHTFLUSH J"));
	assertTrue("Yes String should be valid", ipp_validate_msg(REGEX_MSG_YES, "YES FOUROFAKIND 7 A"));
	assertTrue("Yes String should be valid", ipp_validate_msg(REGEX_MSG_YES, "YES FULLHOUSE Q 3"));
	assertTrue("Yes String should be valid", ipp_validate_msg(REGEX_MSG_YES, "YES FLUSH T 9 8 6 2"));
	assertTrue("Yes String should be valid", ipp_validate_msg(REGEX_MSG_YES, "YES STRAIGHT K"));
	assertTrue("Yes String should be valid", ipp_validate_msg(REGEX_MSG_YES, "YES THREEOFAKIND 2 Q 7"));
	assertTrue("Yes String should be valid", ipp_validate_msg(REGEX_MSG_YES, "YES TWOPAIR J 8 A"));
	assertTrue("Yes String should be valid", ipp_validate_msg(REGEX_MSG_YES, "YES ONEPAIR T A 5 3"));
	assertTrue("Yes String should be valid", ipp_validate_msg(REGEX_MSG_YES, "YES HIGHCARD Q T 8 7 2"));

	assertFalse("Yes String should not be valid", ipp_validate_msg(REGEX_MSG_YES, "YES HIGHCARD Q T 8 7 "));
	assertFalse("Yes String should not be valid", ipp_validate_msg(REGEX_MSG_YES, "YES HIGHCARD Q T 8 7 S"));
	assertFalse("Yes String should not be valid", ipp_validate_msg(REGEX_MSG_YES, "YES FLUSH T 9 8 6 22"));
	assertFalse("Yes String should not be valid", ipp_validate_msg(REGEX_MSG_YES, "W FULLHOUSE Q 3"));
	assertFalse("Yes String should not be valid", ipp_validate_msg(REGEX_MSG_YES, "R 9 8 6 2"));
	assertFalse("Yes String should not be valid", ipp_validate_msg(REGEX_MSG_YES, ""));
	assertFalse("Yes String should not be valid", ipp_validate_msg(REGEX_MSG_YES, "YES X 1 1"));
	return PASS;
}